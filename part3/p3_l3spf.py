"""
L3 Shortest Path First Controller
Implements layer-3 routing with multiple subnets
COL334 Assignment 3 - Part 3
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

class L3SPFController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(L3SPFController, self).__init__(*args, **kwargs)
        
        # Initialize data structures FIRST
        # Datapath management
        self.datapaths = {}  # dpid -> datapath object
        
        # Topology graph for shortest path
        self.graph = nx.Graph()
        
        # Port mappings
        self.link_to_port = {}  # (src_dpid, dst_dpid) -> src_port
        self.port_to_link = {}  # (dpid, port) -> dst_dpid
        
        # IP to MAC/Switch mapping (learned from ARP)
        self.ip_to_mac = {}  # IP -> MAC
        self.ip_to_switch = {}  # IP -> (dpid, port)
        
        # ARP table for switches (switch acts as gateway)
        self.switch_arp_table = {}  # (dpid, ip) -> mac
        
        # Installed paths cache
        self.installed_paths = {}  # (src_ip, dst_ip) -> path
        
        # Load configuration AFTER initializing data structures
        self.load_config('p3_config.json')
        
        self.logger.info("="*60)
        self.logger.info("L3-SPF Controller Started")
        self.logger.info("="*60)

    def load_config(self, config_file):
        """Load network configuration"""
        with open(config_file) as f:
            config = json.load(f)
        
        self.hosts = {}  # host_name -> {ip, mac, switch, subnet}
        self.switches = {}  # dpid -> {interfaces, subnets}
        self.switch_interfaces = {}  # dpid -> {port -> {ip, mac, subnet, neighbor}}
        self.subnet_to_switch = {}  # subnet -> (dpid, port)
        
        # Parse hosts
        for host in config['hosts']:
            self.hosts[host['name']] = {
                'ip': host['ip'],
                'mac': host['mac'],
                'switch': host['switch'],
                'subnet': host['connected_subnet']
            }
            # Map subnet to switch
            switch_dpid = int(host['switch'][1:])
            self.subnet_to_switch[host['connected_subnet']] = switch_dpid
            
            # Pre-populate IP to MAC mapping from config
            self.ip_to_mac[host['ip']] = host['mac']
            self.logger.info("Pre-loaded: %s -> %s on %s", 
                           host['ip'], host['mac'], host['switch'])
        
        # Parse switches and build graph
        for switch in config['switches']:
            dpid = switch['dpid']
            self.switches[dpid] = switch
            self.switch_interfaces[dpid] = {}
            self.graph.add_node(f's{dpid}')
            
            # Process interfaces (note the typo in config: "intesfaces")
            for idx, iface in enumerate(switch.get('intesfaces', switch.get('interfaces', []))):
                # Determine port number from interface name
                # s1-eth1 -> port 1, s1-eth2 -> port 2, etc.
                port = int(iface['name'].split('eth')[1])
                
                self.switch_interfaces[dpid][port] = {
                    'ip': iface['ip'],
                    'mac': iface['mac'],
                    'subnet': iface['subnet'],
                    'neighbor': iface.get('neighbos', iface.get('neighbor', ''))  # Handle typo
                }
                
                # Store switch's own IP/MAC for ARP responses
                self.switch_arp_table[(dpid, iface['ip'])] = iface['mac']
                
                # Check if neighbor is a host and pre-populate ip_to_switch
                neighbor = iface.get('neighbos', iface.get('neighbor', ''))
                if neighbor.startswith('h'):  # It's a host
                    # Find the host's IP
                    for host_name, host_info in self.hosts.items():
                        if host_name == neighbor:
                            host_ip = host_info['ip']
                            self.ip_to_switch[host_ip] = (dpid, port)
                            self.logger.info("Pre-loaded switch mapping: %s -> s%d port %d", 
                                           host_ip, dpid, port)
                            break
        
        # Parse links and add edges to graph
        for link in config['links']:
            # Handle typo in config: "ssc" instead of "src"
            src = link.get('ssc', link.get('src', ''))
            dst = link['dst']
            cost = link['cost']
            
            src_dpid = int(src[1:])
            dst_dpid = int(dst[1:])
            
            self.graph.add_edge(f's{src_dpid}', f's{dst_dpid}', weight=cost)
            self.logger.info("Config edge: s%d <-> s%d (cost=%d)", src_dpid, dst_dpid, cost)
            
        self.logger.info("Loaded config: %d hosts, %d switches", 
                        len(self.hosts), len(self.switches))
        self.logger.info("Graph edges: %s", list(self.graph.edges(data=True)))

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Handle switch connection"""
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id
        
        self.datapaths[dpid] = datapath
        
        # Install table-miss: send to controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        
        # Install ARP handling rule (send ARP to controller)
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_ARP)
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 10, match, actions)
        
        self.logger.info("Switch s%d connected", dpid)

    @set_ev_cls(topo_event.EventLinkAdd)
    def link_add_handler(self, ev):
        """Handle link discovery"""
        link = ev.link
        src_dpid = link.src.dpid
        dst_dpid = link.dst.dpid
        src_port = link.src.port_no
        dst_port = link.dst.port_no
        
        # Check if this port is a host-facing port (from config)
        # Don't add it as an inter-switch link
        src_is_host_port = False
        dst_is_host_port = False
        
        if src_dpid in self.switch_interfaces:
            if src_port in self.switch_interfaces[src_dpid]:
                neighbor = self.switch_interfaces[src_dpid][src_port].get('neighbor', '')
                if neighbor.startswith('h'):  # Host port
                    src_is_host_port = True
        
        if dst_dpid in self.switch_interfaces:
            if dst_port in self.switch_interfaces[dst_dpid]:
                neighbor = self.switch_interfaces[dst_dpid][dst_port].get('neighbor', '')
                if neighbor.startswith('h'):  # Host port
                    dst_is_host_port = True
        
        # Skip if either end is a host port
        if src_is_host_port or dst_is_host_port:
            self.logger.info("Ignoring link (host port): s%d port %d <-> s%d port %d",
                           src_dpid, src_port, dst_dpid, dst_port)
            return
        
        # Store bidirectional link info
        self.link_to_port[(src_dpid, dst_dpid)] = src_port
        self.link_to_port[(dst_dpid, src_dpid)] = dst_port
        self.port_to_link[(src_dpid, src_port)] = dst_dpid
        self.port_to_link[(dst_dpid, dst_port)] = src_dpid
        
        self.logger.info("Link: s%d port %d <-> s%d port %d",
                        src_dpid, src_port, dst_dpid, dst_port)

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
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']
        dpid = datapath.id
        
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        
        # Handle ARP
        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            self.handle_arp(datapath, in_port, pkt, eth)
            return
        
        # Handle IPv4
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
        
        # Learn source IP to MAC mapping
        if src_ip not in self.ip_to_mac:
            self.ip_to_mac[src_ip] = src_mac
            self.ip_to_switch[src_ip] = (dpid, in_port)
            self.logger.info("Learned: %s -> %s (s%d port %d)", 
                           src_ip, src_mac, dpid, in_port)
        
        # ARP Request
        if arp_pkt.opcode == arp.ARP_REQUEST:
            self.logger.info("ARP Request: Who has %s? Tell %s (s%d port %d)",
                           dst_ip, src_ip, dpid, in_port)
            
            # Check if we (switch) should respond
            # Switch responds if dst_ip is its gateway IP
            reply_mac = None
            for port, iface in self.switch_interfaces.get(dpid, {}).items():
                if iface['ip'] == dst_ip:
                    reply_mac = iface['mac']
                    self.logger.info("Switch s%d responding to ARP for %s with MAC %s",
                                   dpid, dst_ip, reply_mac)
                    break
            
            if reply_mac:
                # Send ARP reply
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
                self.logger.info("Sent ARP Reply: %s is at %s", dst_ip, reply_mac)
            else:
                # Flood if we don't know
                self.logger.info("Flooding ARP request for %s", dst_ip)
                self.flood_packet(datapath, pkt, in_port)
        
        # ARP Reply
        elif arp_pkt.opcode == arp.ARP_REPLY:
            self.logger.info("ARP Reply: %s is at %s", src_ip, src_mac)
            # Learn and flood (or could forward based on pending requests)
            self.flood_packet(datapath, pkt, in_port)

    def handle_ipv4(self, datapath, in_port, msg, pkt, eth):
        """Handle IPv4 packets with L3 routing"""
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if not ip_pkt:
            return
        
        dpid = datapath.id
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        
        src_ip = ip_pkt.src
        dst_ip = ip_pkt.dst
        ttl = ip_pkt.ttl
        
        self.logger.info("IP packet: %s -> %s (TTL=%d) on s%d port %d",
                        src_ip, dst_ip, ttl, dpid, in_port)
        
        # Check TTL
        if ttl <= 1:
            self.logger.warning("TTL expired for packet %s -> %s", src_ip, dst_ip)
            # Should send ICMP Time Exceeded, but for simplicity we drop
            return
        
        # Check if destination MAC is known
        if dst_ip not in self.ip_to_mac:
            self.logger.warning("Unknown destination IP %s, dropping", dst_ip)
            return
        
        dst_mac = self.ip_to_mac[dst_ip]
        dst_dpid, dst_port = self.ip_to_switch[dst_ip]
        
        # Learn source if not known
        if src_ip not in self.ip_to_mac:
            self.ip_to_mac[src_ip] = eth.src
            self.ip_to_switch[src_ip] = (dpid, in_port)
        
        # Always compute path from CURRENT switch to destination
        # (packet might arrive at any switch due to flooding)
        src_node = f's{dpid}'
        dst_node = f's{dst_dpid}'
        
        try:
            path = nx.shortest_path(self.graph, src_node, dst_node, weight='weight')
            self.logger.info("Path from s%d to s%d: %s", dpid, dst_dpid, path)
            
            # Install rules on this path if not already done
            path_key = (dpid, dst_ip)  # Key by current switch and destination
            if path_key not in self.installed_paths:
                self.installed_paths[path_key] = path
                # Install L3 forwarding rules along the path
                self.install_l3_path(path, src_ip, dst_ip, dst_mac, dst_port, ttl)
            
        except nx.NetworkXNoPath:
            self.logger.error("No path from s%d to s%d", dpid, dst_dpid)
            return
        except Exception as e:
            self.logger.error("Path computation error: %s", str(e))
            import traceback
            traceback.print_exc()
            return
        
        # Forward the current packet using the installed rules
        path = self.installed_paths[path_key]
        out_port = self.get_output_port(dpid, path, dst_port)
        
        if out_port is None:
            self.logger.error("Cannot determine output port on s%d for path %s", dpid, path)
            return
        
        self.logger.info("Forwarding packet on s%d to port %d (path: %s)", dpid, out_port, path)
        
        # Rewrite Ethernet header and decrement TTL
        actions = []
        
        # Get the next hop MAC address
        next_hop_mac = self.get_next_hop_mac(dpid, out_port)
        if next_hop_mac:
            actions.append(parser.OFPActionSetField(eth_dst=next_hop_mac))
            self.logger.debug("Setting eth_dst to %s", next_hop_mac)
        else:
            # Last hop to host, use host MAC
            actions.append(parser.OFPActionSetField(eth_dst=dst_mac))
            self.logger.debug("Setting eth_dst to host MAC %s", dst_mac)
        
        # Set source MAC to this switch's outgoing interface MAC
        if dpid in self.switch_interfaces and out_port in self.switch_interfaces[dpid]:
            src_mac = self.switch_interfaces[dpid][out_port]['mac']
            actions.append(parser.OFPActionSetField(eth_src=src_mac))
            self.logger.debug("Setting eth_src to %s", src_mac)
        
        # Decrement TTL
        actions.append(parser.OFPActionDecNwTtl())
        actions.append(parser.OFPActionOutput(out_port))
        
        # Send packet out
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
        """Install L3 forwarding rules along the path"""
        for i, switch_name in enumerate(path):
            switch_id = int(switch_name[1:])
            
            if switch_id not in self.datapaths:
                self.logger.warning("Switch s%d not connected yet", switch_id)
                continue
            
            datapath = self.datapaths[switch_id]
            parser = datapath.ofproto_parser
            
            # Determine output port
            if i < len(path) - 1:
                next_switch_id = int(path[i+1][1:])
                out_port = self.link_to_port.get((switch_id, next_switch_id))
                if out_port is None:
                    self.logger.error("No port mapping s%d -> s%d", switch_id, next_switch_id)
                    continue
            else:
                # Last switch in path
                out_port = final_port
            
            # Build match for IP destination (high priority to override table-miss)
            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_dst=dst_ip)
            
            # Build actions: rewrite MAC, decrement TTL, output
            actions = []
            
            # Last hop: set destination MAC to actual host MAC
            if i == len(path) - 1:
                actions.append(parser.OFPActionSetField(eth_dst=dst_mac))
                self.logger.debug("s%d: Setting dst MAC to host %s", switch_id, dst_mac)
            else:
                # Intermediate hop: set dst MAC to next switch's interface MAC
                next_hop_mac = self.get_next_hop_mac(switch_id, out_port)
                if next_hop_mac:
                    actions.append(parser.OFPActionSetField(eth_dst=next_hop_mac))
                    self.logger.debug("s%d: Setting dst MAC to next switch %s", 
                                    switch_id, next_hop_mac)
            
            # Set source MAC to this switch's outgoing interface
            if switch_id in self.switch_interfaces and out_port in self.switch_interfaces[switch_id]:
                src_mac = self.switch_interfaces[switch_id][out_port]['mac']
                actions.append(parser.OFPActionSetField(eth_src=src_mac))
                self.logger.debug("s%d: Setting src MAC to %s", switch_id, src_mac)
            
            # Decrement TTL
            actions.append(parser.OFPActionDecNwTtl())
            
            # Output to port
            actions.append(parser.OFPActionOutput(out_port))
            
            # Install flow with high priority (no timeout for persistent rules)
            self.add_flow(datapath, 100, match, actions, idle_timeout=0, hard_timeout=0)
            self.logger.info("Installed L3 rule on s%d: %s -> port %d", 
                           switch_id, dst_ip, out_port)

    def get_output_port(self, switch_id, path, final_port):
        """Get output port for a switch given the path"""
        switch_name = f's{switch_id}'
        if switch_name not in path:
            return None
        
        idx = path.index(switch_name)
        if idx < len(path) - 1:
            next_id = int(path[idx+1][1:])
            return self.link_to_port.get((switch_id, next_id))
        else:
            return final_port

    def get_next_hop_mac(self, switch_id, out_port):
        """Get MAC address of next hop interface"""
        # Check if it's a link to another switch
        if (switch_id, out_port) in self.port_to_link:
            next_switch_id = self.port_to_link[(switch_id, out_port)]
            # Find the port on the next switch that connects back
            reverse_port = self.link_to_port.get((next_switch_id, switch_id))
            if reverse_port and next_switch_id in self.switch_interfaces:
                if reverse_port in self.switch_interfaces[next_switch_id]:
                    mac = self.switch_interfaces[next_switch_id][reverse_port]['mac']
                    self.logger.debug("Next hop MAC for s%d port %d: %s (s%d port %d)",
                                    switch_id, out_port, mac, next_switch_id, reverse_port)
                    return mac
        
        # It's a host port - the actual host MAC will be used
        self.logger.debug("Next hop for s%d port %d is a host", switch_id, out_port)
        return None

    def flood_packet(self, datapath, pkt, in_port):
        """Flood packet to all ports except in_port"""
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