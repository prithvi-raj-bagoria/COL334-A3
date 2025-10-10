"""
L2 Shortest Path First Controller with Per-Flow ECMP
Includes automatic topology discovery via LLDP
COL334 Assignment 3 - Part 2
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, tcp, lldp
from ryu.topology import event as topo_event
from ryu.topology.api import get_switch, get_link
import networkx as nx
import json
import random


class L2SPFController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(L2SPFController, self).__init__(*args, **kwargs)
        
        # Load configuration
        self.load_config('config.json')
        
        # MAC learning tables
        self.mac_to_switch = {}  # MAC -> switch dpid
        self.mac_to_port = {}    # dpid -> {MAC -> port}
        
        # Datapath management
        self.datapaths = {}  # dpid -> datapath object
        
        # Flow tracking for per-flow ECMP
        self.flow_to_path = {}  # (src_mac, dst_mac) -> chosen path
        
        # Topology discovery (auto-populated by Ryu)
        self.topology_graph = nx.Graph()
        self.link_to_port = {}  # (src_dpid, dst_dpid) -> src_port
        self.port_to_link = {}  # (dpid, port) -> neighbor_dpid
        
        self.logger.info("="*60)
        self.logger.info("L2-SPF Controller Started")
        self.logger.info("ECMP: %s", self.ecmp_enabled)
        self.logger.info("="*60)

    def load_config(self, config_file):
        """Load topology configuration"""
        with open(config_file) as f:
            self.config = json.load(f)
        
        # Build NetworkX graph from config (for weights)
        self.graph = nx.Graph()
        nodes = self.config['nodes']
        weights = self.config['weight_matrix']
        
        for i, src in enumerate(nodes):
            for j, dst in enumerate(nodes):
                if weights[i][j] > 0:
                    self.graph.add_edge(src, dst, weight=weights[i][j])
        
        self.ecmp_enabled = self.config.get('ecmp', False)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Handle switch connection"""
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id

        self.datapaths[dpid] = datapath
        
        if dpid not in self.mac_to_port:
            self.mac_to_port[dpid] = {}

        # Install table-miss: send to controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        
        self.logger.info("Switch s%d connected", dpid)

    @set_ev_cls(topo_event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        """Handle switch topology changes"""
        self.logger.info("Topology change detected, rebuilding...")
        self.discover_topology()

    @set_ev_cls(topo_event.EventLinkAdd)
    def link_add_handler(self, ev):
        """Handle link addition"""
        link = ev.link
        src_dpid = link.src.dpid
        dst_dpid = link.dst.dpid
        src_port = link.src.port_no
        dst_port = link.dst.port_no
        
        # Store bidirectional link info
        self.link_to_port[(src_dpid, dst_dpid)] = src_port
        self.link_to_port[(dst_dpid, src_dpid)] = dst_port
        self.port_to_link[(src_dpid, src_port)] = dst_dpid
        self.port_to_link[(dst_dpid, dst_port)] = src_dpid
        
        self.logger.info("Link discovered: s%d port %d <-> s%d port %d",
                        src_dpid, src_port, dst_dpid, dst_port)

    def discover_topology(self):
        """Discover network topology using Ryu's topology API"""
        switches = get_switch(self)
        links = get_link(self)
        
        # Clear old topology
        self.topology_graph.clear()
        self.link_to_port.clear()
        self.port_to_link.clear()
        
        # Add switches
        for switch in switches:
            self.topology_graph.add_node(f's{switch.dp.id}')
        
        # Add links
        for link in links:
            src = f's{link.src.dpid}'
            dst = f's{link.dst.dpid}'
            
            # Get weight from config
            weight = self.graph[src][dst]['weight'] if self.graph.has_edge(src, dst) else 1
            
            self.topology_graph.add_edge(src, dst, weight=weight)
            
            # Store port mappings
            self.link_to_port[(link.src.dpid, link.dst.dpid)] = link.src.port_no
            self.port_to_link[(link.src.dpid, link.src.port_no)] = link.dst.dpid
        
        self.logger.info("Topology: %d switches, %d links",
                        self.topology_graph.number_of_nodes(),
                        self.topology_graph.number_of_edges())

    def add_flow(self, datapath, priority, match, actions, idle_timeout=0):
        """Install flow rule"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout
        )
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """Handle PacketIn events"""
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']
        dpid = datapath.id
    
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
    
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
    
        src = eth.src
        dst = eth.dst
    
        # Ignore multicast/broadcast
        if dst.startswith('33:33') or dst.startswith('01:00:5e') or dst.startswith('ff:ff'):
            return
    
        # Learn source MAC (only from host ports)
        if (dpid, in_port) not in self.port_to_link:
            self.mac_to_switch[src] = dpid
            self.mac_to_port[dpid][src] = in_port
            self.logger.info("Host MAC %s at s%d port %d", src, dpid, in_port)
    
        # Handle ARP
        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            self.flood_packet(datapath, msg, in_port)
            return
    
        # Check if we know destination
        if dst not in self.mac_to_switch:
            self.flood_packet(datapath, msg, in_port)
            return
    
        # We know both src and dst
        dst_dpid = self.mac_to_switch[dst]
        dst_port = self.mac_to_port[dst_dpid][dst]
    
        # Same switch?
        if dpid == dst_dpid:
            actions = [parser.OFPActionOutput(dst_port)]
            data = None
            if msg.buffer_id == ofproto.OFP_NO_BUFFER:
                data = msg.data
            out = parser.OFPPacketOut(
                datapath=datapath, buffer_id=msg.buffer_id,
                in_port=in_port, actions=actions, data=data)
            datapath.send_msg(out)
            return
    
        # Different switches - need path
        # KEY FIX: Use (current_switch, dst_switch, dst_mac) as key
        path_key = (dpid, dst_dpid, dst)  # ← CHANGED FROM (src, dst)
        
        if path_key not in self.flow_to_path:
            # Compute path from CURRENT switch to destination
            src_node = f's{dpid}'  # ← Use current switch!
            dst_node = f's{dst_dpid}'
            
            try:
                graph_to_use = self.topology_graph if self.topology_graph.number_of_nodes() > 0 else self.graph
                
                all_paths = list(nx.all_shortest_paths(
                    graph_to_use, source=src_node, target=dst_node, weight='weight'))
                
                if self.ecmp_enabled and len(all_paths) > 1:
                    path = random.choice(all_paths)
                    self.logger.info("ECMP: %s (%d paths) -> %s",
                                   src_node, len(all_paths), path)
                else:
                    path = all_paths[0]
                    self.logger.info("Path: %s -> %s", src_node, path)
                
                self.flow_to_path[path_key] = path
                
                # Parse TCP
                ip_pkt = pkt.get_protocol(ipv4.ipv4)
                tcp_pkt = pkt.get_protocol(tcp.tcp)
                
                # Install rules
                self.install_path_rules(path, dst, dst_port, tcp_pkt)
                
            except nx.NetworkXNoPath:
                self.logger.error("No path s%d -> s%d", dpid, dst_dpid)
                return
            except Exception as e:
                self.logger.error("Path computation error: %s", str(e))
                return
    
        # Forward this packet
        path = self.flow_to_path[path_key]
        out_port = self.get_outport(dpid, path, dst_port)
        
        if out_port is None:
            self.logger.warning("Cannot forward on s%d, flooding", dpid)
            self.flood_packet(datapath, msg, in_port)
            return
        
        actions = [parser.OFPActionOutput(out_port)]
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)
    
      



    def flood_packet(self, datapath, msg, in_port):
        """Flood a packet"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    def install_path_rules(self, path, dst_mac, final_port, tcp_pkt):
        """Install flow rules on all switches in path"""
        self.logger.info("Installing rules for %s on %s", dst_mac, path)

        rules_installed = 0  # ← ADD THIS

        for i, switch_name in enumerate(path):
            switch_id = int(switch_name[1:])

            if switch_id not in self.datapaths:
                self.logger.warning("Switch s%d not in datapaths!", switch_id)  # ← ADD THIS
                continue
            
            datapath = self.datapaths[switch_id]
            parser = datapath.ofproto_parser

            # Determine output port
            if i < len(path) - 1:
                next_switch_id = int(path[i+1][1:])
                out_port = self.link_to_port.get((switch_id, next_switch_id))

                if out_port is None:
                    self.logger.error("No port info for s%d -> s%d", switch_id, next_switch_id)
                    continue
            else:
                out_port = final_port

            # Create match
            if tcp_pkt:
                match = parser.OFPMatch(
                    eth_type=0x0800,
                    eth_dst=dst_mac,
                    ip_proto=6,
                    tcp_src=tcp_pkt.src_port,
                    tcp_dst=tcp_pkt.dst_port
                )
                priority = 10
                self.logger.info("  s%d: TCP dst=%s [%d:%d] -> port %d",
                               switch_id, dst_mac, tcp_pkt.src_port,
                               tcp_pkt.dst_port, out_port)
            else:
                match = parser.OFPMatch(eth_dst=dst_mac)
                priority = 5
                self.logger.info("  s%d: MAC dst=%s -> port %d",
                               switch_id, dst_mac, out_port)

            actions = [parser.OFPActionOutput(out_port)]
            self.add_flow(datapath, priority, match, actions, idle_timeout=30)
            rules_installed += 1  # ← ADD THIS

        self.logger.info("Installed %d rules on path", rules_installed)  # ← ADD THIS


    def get_outport(self, switch_id, path, final_port):
        """Get output port for switch given path"""
        switch_name = f's{switch_id}'

        # Check if this switch is in the path
        if switch_name not in path:
            self.logger.error("Switch %s not in path %s", switch_name, path)
            return None  # ← Return None instead of crashing

        idx = path.index(switch_name)

        if idx < len(path) - 1:
            next_id = int(path[idx+1][1:])
            port = self.link_to_port.get((switch_id, next_id))
            if port is None:
                self.logger.error("No port mapping for s%d -> s%d", switch_id, next_id)
                return None
            return port
        else:
            return final_port
