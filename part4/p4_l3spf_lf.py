"""
L3 Shortest Path First Controller with Link Failure Handling (CLEAN OUTPUT)
Implements layer-3 routing with dynamic path recomputation on link failures
Filters out topology discovery noise during initialization
COL334 Assignment 3 - Part 4
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, arp, ipv4, icmp
from ryu.topology import event as topo_event
from ryu.topology.api import get_switch, get_link
import networkx as nx
import json
import ipaddress
import time


class L3SPFLinkFailureController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    
    def __init__(self, *args, **kwargs):
        super(L3SPFLinkFailureController, self).__init__(*args, **kwargs)
        
        # ===== WARM-UP FILTER (NEW) =====
        self.start_time = time.time()
        self.warmup_period = 20  # Ignore first 20 seconds
        self.topology_ready = False
        # ================================
        
        # Initialize data structures
        self.datapaths = {}
        self.graph = nx.Graph()
        self.original_graph = nx.Graph()
        
        # Port mappings
        self.link_to_port = {}
        self.port_to_link = {}
        
        # IP to MAC/Switch mapping
        self.ip_to_mac = {}
        self.ip_to_switch = {}
        self.switch_arp_table = {}
        
        # Installed paths cache
        self.installed_paths = {}
        
        # Link failure tracking
        self.failed_links = set()
        self.link_costs = {}
        
        # Convergence measurement
        self.last_failure_time = None
        self.last_recovery_time = None
        
        # Load configuration
        self.load_config('p4_config.json')
        
        self.logger.info("="*60)
        self.logger.info("L3-SPF Controller with Link Failure Handling Started")
        self.logger.info("Warm-up period: %d seconds (filtering topology discovery)", self.warmup_period)
        self.logger.info("="*60)
    
    def _is_warmup_period(self):
        """Check if still in warm-up period"""
        return (time.time() - self.start_time) < self.warmup_period
    
    def _check_and_announce_ready(self):
        """Announce when topology is ready"""
        if not self.topology_ready and not self._is_warmup_period():
            self.topology_ready = True
            self.logger.info("="*60)
            self.logger.info("TOPOLOGY READY - Now monitoring for link failures")
            self.logger.info("="*60)
    
    def load_config(self, config_file):
        """Load network configuration"""
        with open(config_file) as f:
            config = json.load(f)
        
        self.hosts = {}
        self.switches = {}
        self.switch_interfaces = {}
        self.subnet_to_switch = {}
        
        # Parse hosts
        for host in config['hosts']:
            self.hosts[host['name']] = {
                'ip': host['ip'],
                'mac': host['mac'],
                'switch': host['switch'],
                'subnet': host['connected_subnet']
            }
            
            switch_dpid = int(host['switch'][1:])
            self.subnet_to_switch[host['connected_subnet']] = switch_dpid
            self.ip_to_mac[host['ip']] = host['mac']
            self.logger.info("Pre-loaded: %s -> %s on %s",
                           host['ip'], host['mac'], host['switch'])
        
        # Parse switches
        for switch in config['switches']:
            dpid = switch['dpid']
            self.switches[dpid] = switch
            self.switch_interfaces[dpid] = {}
            self.graph.add_node(f's{dpid}')
            self.original_graph.add_node(f's{dpid}')
            
            for iface in switch.get('interfaces', []):
                port = int(iface['name'].split('eth')[1])
                self.switch_interfaces[dpid][port] = {
                    'ip': iface['ip'],
                    'mac': iface['mac'],
                    'subnet': iface['subnet'],
                    'neighbor': iface.get('neighbor', '')
                }
                
                self.switch_arp_table[(dpid, iface['ip'])] = iface['mac']
                
                neighbor = iface.get('neighbor', '')
                if neighbor.startswith('h'):
                    for host_name, host_info in self.hosts.items():
                        if host_name == neighbor:
                            host_ip = host_info['ip']
                            self.ip_to_switch[host_ip] = (dpid, port)
                            break
        
        # Parse links
        for link in config['links']:
            src_dpid = int(link['src'][1:])
            dst_dpid = int(link['dst'][1:])
            cost = link['cost']
            
            self.graph.add_edge(f's{src_dpid}', f's{dst_dpid}', weight=cost)
            self.original_graph.add_edge(f's{src_dpid}', f's{dst_dpid}', weight=cost)
            
            self.link_costs[(src_dpid, dst_dpid)] = cost
            self.link_costs[(dst_dpid, src_dpid)] = cost
            
            self.logger.info("Config edge: s%d <-> s%d (cost=%d)", src_dpid, dst_dpid, cost)
        
        self.logger.info("Loaded: %d hosts, %d switches", len(self.hosts), len(self.switches))
    
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Handle switch connection"""
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id
        
        self.datapaths[dpid] = datapath
        
        # Install table-miss
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        
        # Install ARP handling
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_ARP)
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 10, match, actions)
        
        self.logger.info("Switch s%d connected", dpid)
    
    @set_ev_cls(topo_event.EventLinkAdd)
    def link_add_handler(self, ev):
        """Handle link discovery or recovery"""
        self._check_and_announce_ready()
        
        link = ev.link
        src_dpid = link.src.dpid
        dst_dpid = link.dst.dpid
        src_port = link.src.port_no
        dst_port = link.dst.port_no
        
        # Check if host-facing port
        src_is_host_port = False
        dst_is_host_port = False
        
        if src_dpid in self.switch_interfaces:
            if src_port in self.switch_interfaces[src_dpid]:
                neighbor = self.switch_interfaces[src_dpid][src_port].get('neighbor', '')
                if neighbor.startswith('h'):
                    src_is_host_port = True
        
        if dst_dpid in self.switch_interfaces:
            if dst_port in self.switch_interfaces[dst_dpid]:
                neighbor = self.switch_interfaces[dst_dpid][dst_port].get('neighbor', '')
                if neighbor.startswith('h'):
                    dst_is_host_port = True
        
        if src_is_host_port or dst_is_host_port:
            return
        
        # Store link info
        self.link_to_port[(src_dpid, dst_dpid)] = src_port
        self.link_to_port[(dst_dpid, src_dpid)] = dst_port
        self.port_to_link[(src_dpid, src_port)] = dst_dpid
        self.port_to_link[(dst_dpid, dst_port)] = src_dpid
        
        # Check if recovery
        link_tuple = (min(src_dpid, dst_dpid), max(src_dpid, dst_dpid))
        if link_tuple in self.failed_links:
            # ===== FILTER: Only print after warm-up =====
            if not self._is_warmup_period():
                self.last_recovery_time = time.time()
                self.logger.warning("="*60)
                self.logger.warning("LINK RECOVERY: s%d <-> s%d", src_dpid, dst_dpid)
                self.logger.warning("="*60)
            # ============================================
            
            self.failed_links.remove(link_tuple)
            
            # Restore in graph
            src_node = f's{src_dpid}'
            dst_node = f's{dst_dpid}'
            cost = self.link_costs.get((src_dpid, dst_dpid), 10)
            
            if not self.graph.has_edge(src_node, dst_node):
                self.graph.add_edge(src_node, dst_node, weight=cost)
            
            # Recompute paths
            self.recompute_all_paths()
        else:
            # ===== FILTER: Silently track during warm-up =====
            if not self._is_warmup_period():
                self.logger.info("Link: s%d port %d <-> s%d port %d",
                               src_dpid, src_port, dst_dpid, dst_port)
            # =================================================
    
    @set_ev_cls(topo_event.EventLinkDelete, MAIN_DISPATCHER)
    def link_delete_handler(self, ev):
        """Handle link failure"""
        self._check_and_announce_ready()
        
        link = ev.link
        src_dpid = link.src.dpid
        dst_dpid = link.dst.dpid
        src_port = link.src.port_no
        dst_port = link.dst.port_no
        
        # ===== FILTER: Only process after warm-up =====
        if self._is_warmup_period():
            # Silently update graph
            link_tuple = (min(src_dpid, dst_dpid), max(src_dpid, dst_dpid))
            self.failed_links.add(link_tuple)
            
            src_node = f's{src_dpid}'
            dst_node = f's{dst_dpid}'
            if self.graph.has_edge(src_node, dst_node):
                self.graph.remove_edge(src_node, dst_node)
            
            self.link_to_port.pop((src_dpid, dst_dpid), None)
            self.link_to_port.pop((dst_dpid, src_dpid), None)
            self.port_to_link.pop((src_dpid, src_port), None)
            self.port_to_link.pop((dst_dpid, dst_port), None)
            return
        # ==============================================
        
        self.last_failure_time = time.time()
        self.logger.warning("="*60)
        self.logger.warning("LINK FAILURE: s%d <-> s%d", src_dpid, dst_dpid)
        self.logger.warning("="*60)
        
        # Mark failed
        link_tuple = (min(src_dpid, dst_dpid), max(src_dpid, dst_dpid))
        self.failed_links.add(link_tuple)
        
        # Remove from graph
        src_node = f's{src_dpid}'
        dst_node = f's{dst_dpid}'
        if self.graph.has_edge(src_node, dst_node):
            self.graph.remove_edge(src_node, dst_node)
        
        # Clear mappings
        self.link_to_port.pop((src_dpid, dst_dpid), None)
        self.link_to_port.pop((dst_dpid, src_dpid), None)
        self.port_to_link.pop((src_dpid, src_port), None)
        self.port_to_link.pop((dst_dpid, dst_port), None)
        
        # Recompute
        self.recompute_all_paths()
    
    def recompute_all_paths(self):
        """Recompute all paths and reinstall flows"""
        self.logger.info("="*60)
        self.logger.info("RECOMPUTING ALL PATHS")
        self.logger.info("="*60)
        
        # Clear flows
        for dpid, datapath in self.datapaths.items():
            self.clear_flows(datapath)
            
            # Reinstall basic rules
            ofproto = datapath.ofproto
            parser = datapath.ofproto_parser
            
            match = parser.OFPMatch()
            actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                              ofproto.OFPCML_NO_BUFFER)]
            self.add_flow(datapath, 0, match, actions)
            
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_ARP)
            actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                              ofproto.OFPCML_NO_BUFFER)]
            self.add_flow(datapath, 10, match, actions)
        
        # Reinstall paths
        old_paths = self.installed_paths.copy()
        self.installed_paths.clear()
        
        for (src_dpid, dst_ip), old_path in old_paths.items():
            if dst_ip not in self.ip_to_mac or dst_ip not in self.ip_to_switch:
                continue
            
            dst_mac = self.ip_to_mac[dst_ip]
            dst_dpid, dst_port = self.ip_to_switch[dst_ip]
            
            src_node = f's{src_dpid}'
            dst_node = f's{dst_dpid}'
            
            try:
                new_path = nx.shortest_path(self.graph, src_node, dst_node, weight='weight')
                self.logger.info("New path s%d -> %s: %s", src_dpid, dst_ip, new_path)
                
                self.installed_paths[(src_dpid, dst_ip)] = new_path
                self.install_l3_path(new_path, None, dst_ip, dst_mac, dst_port, 64)
            except nx.NetworkXNoPath:
                self.logger.error("No path from s%d to s%d after failure", src_dpid, dst_dpid)
        
        # Calculate convergence time
        convergence_time = None
        if self.last_failure_time:
            convergence_time = time.time() - self.last_failure_time
        elif self.last_recovery_time:
            convergence_time = time.time() - self.last_recovery_time
        
        if convergence_time:
            self.logger.warning("CONVERGENCE TIME: %.3f seconds", convergence_time)
    
    def clear_flows(self, datapath):
        """Clear all flows"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        match = parser.OFPMatch()
        mod = parser.OFPFlowMod(
            datapath=datapath,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            match=match)
        datapath.send_msg(mod)
    
    def add_flow(self, datapath, priority, match, actions, idle_timeout=0, hard_timeout=0):
        """Install flow rule"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout)
        datapath.send_msg(mod)
    
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """Handle PacketIn events"""
        msg = ev.msg
        datapath = msg.datapath
        in_port = msg.match['in_port']
        
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        
        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            self.handle_arp(datapath, in_port, pkt, eth)
            return
        
        if eth.ethertype == ether_types.ETH_TYPE_IP:
            self.handle_ipv4(datapath, in_port, msg, pkt, eth)
            return
    
    def handle_arp(self, datapath, in_port, pkt, eth):
        """Handle ARP packets"""
        arp_pkt = pkt.get_protocol(arp.arp)
        if not arp_pkt:
            return
        
        dpid = datapath.id
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        
        src_ip = arp_pkt.src_ip
        dst_ip = arp_pkt.dst_ip
        src_mac = arp_pkt.src_mac
        
        # Learn source
        if src_ip not in self.ip_to_mac:
            self.ip_to_mac[src_ip] = src_mac
            self.ip_to_switch[src_ip] = (dpid, in_port)
        
        # ARP Request
        if arp_pkt.opcode == arp.ARP_REQUEST:
            reply_mac = None
            for port, iface in self.switch_interfaces.get(dpid, {}).items():
                if iface['ip'] == dst_ip:
                    reply_mac = iface['mac']
                    break
            
            if reply_mac:
                # Send reply
                arp_reply = packet.Packet()
                arp_reply.add_protocol(ethernet.ethernet(
                    ethertype=ether_types.ETH_TYPE_ARP,
                    dst=src_mac,
                    src=reply_mac))
                arp_reply.add_protocol(arp.arp(
                    opcode=arp.ARP_REPLY,
                    src_mac=reply_mac,
                    src_ip=dst_ip,
                    dst_mac=src_mac,
                    dst_ip=src_ip))
                arp_reply.serialize()
                
                actions = [parser.OFPActionOutput(in_port)]
                out = parser.OFPPacketOut(
                    datapath=datapath,
                    buffer_id=ofproto.OFP_NO_BUFFER,
                    in_port=ofproto.OFPP_CONTROLLER,
                    actions=actions,
                    data=arp_reply.data)
                datapath.send_msg(out)
            else:
                self.flood_packet(datapath, pkt, in_port)
        elif arp_pkt.opcode == arp.ARP_REPLY:
            self.flood_packet(datapath, pkt, in_port)
    
    def handle_ipv4(self, datapath, in_port, msg, pkt, eth):
        """Handle IPv4 packets"""
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if not ip_pkt:
            return
        
        dpid = datapath.id
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        
        src_ip = ip_pkt.src
        dst_ip = ip_pkt.dst
        ttl = ip_pkt.ttl
        
        if ttl <= 1:
            return
        
        if dst_ip not in self.ip_to_mac:
            return
        
        dst_mac = self.ip_to_mac[dst_ip]
        dst_dpid, dst_port = self.ip_to_switch[dst_ip]
        
        # Learn source
        if src_ip not in self.ip_to_mac:
            self.ip_to_mac[src_ip] = eth.src
            self.ip_to_switch[src_ip] = (dpid, in_port)
        
        # Compute path
        src_node = f's{dpid}'
        dst_node = f's{dst_dpid}'
        
        try:
            path = nx.shortest_path(self.graph, src_node, dst_node, weight='weight')
            
            path_key = (dpid, dst_ip)
            if path_key not in self.installed_paths:
                self.installed_paths[path_key] = path
                self.install_l3_path(path, src_ip, dst_ip, dst_mac, dst_port, ttl)
        except nx.NetworkXNoPath:
            return
        
        # Forward packet
        path = self.installed_paths[path_key]
        out_port = self.get_output_port(dpid, path, dst_port)
        if out_port is None:
            return
        
        actions = []
        next_hop_mac = self.get_next_hop_mac(dpid, out_port)
        if next_hop_mac:
            actions.append(parser.OFPActionSetField(eth_dst=next_hop_mac))
        else:
            actions.append(parser.OFPActionSetField(eth_dst=dst_mac))
        
        if dpid in self.switch_interfaces and out_port in self.switch_interfaces[dpid]:
            src_mac = self.switch_interfaces[dpid][out_port]['mac']
            actions.append(parser.OFPActionSetField(eth_src=src_mac))
        
        actions.append(parser.OFPActionDecNwTtl())
        actions.append(parser.OFPActionOutput(out_port))
        
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data)
        datapath.send_msg(out)
    
    def install_l3_path(self, path, src_ip, dst_ip, dst_mac, final_port, original_ttl):
        """Install L3 forwarding rules"""
        for i, switch_name in enumerate(path):
            switch_id = int(switch_name[1:])
            if switch_id not in self.datapaths:
                continue
            
            datapath = self.datapaths[switch_id]
            parser = datapath.ofproto_parser
            
            if i < len(path) - 1:
                next_switch_id = int(path[i+1][1:])
                out_port = self.link_to_port.get((switch_id, next_switch_id))
                if out_port is None:
                    continue
            else:
                out_port = final_port
            
            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_dst=dst_ip)
            
            actions = []
            if i == len(path) - 1:
                actions.append(parser.OFPActionSetField(eth_dst=dst_mac))
            else:
                next_hop_mac = self.get_next_hop_mac(switch_id, out_port)
                if next_hop_mac:
                    actions.append(parser.OFPActionSetField(eth_dst=next_hop_mac))
            
            if switch_id in self.switch_interfaces and out_port in self.switch_interfaces[switch_id]:
                src_mac = self.switch_interfaces[switch_id][out_port]['mac']
                actions.append(parser.OFPActionSetField(eth_src=src_mac))
            
            actions.append(parser.OFPActionDecNwTtl())
            actions.append(parser.OFPActionOutput(out_port))
            
            self.add_flow(datapath, 100, match, actions)
    
    def get_output_port(self, switch_id, path, final_port):
        """Get output port for switch in path"""
        switch_name = f's{switch_id}'
        if switch_name not in path:
            return None
        idx = path.index(switch_name)
        if idx < len(path) - 1:
            next_id = int(path[idx+1][1:])
            return self.link_to_port.get((switch_id, next_id))
        return final_port
    
    def get_next_hop_mac(self, switch_id, out_port):
        """Get MAC of next hop"""
        if (switch_id, out_port) in self.port_to_link:
            next_switch_id = self.port_to_link[(switch_id, out_port)]
            reverse_port = self.link_to_port.get((next_switch_id, switch_id))
            if reverse_port and next_switch_id in self.switch_interfaces:
                if reverse_port in self.switch_interfaces[next_switch_id]:
                    return self.switch_interfaces[next_switch_id][reverse_port]['mac']
        return None
    
    def flood_packet(self, datapath, pkt, in_port):
        """Flood packet"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=in_port,
            actions=actions,
            data=pkt.data)
        datapath.send_msg(out)
