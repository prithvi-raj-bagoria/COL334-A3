"""
L2 SPF Controller with Dynamic Real-Time Path Rerouting (UDP ONLY) - FIXED
Continuously monitors flows and migrates them to lighter paths
COL334 Assignment 3 - BONUS
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, udp, lldp
from ryu.topology import event as topo_event
from ryu.topology.api import get_switch, get_link
from ryu.lib import hub
import networkx as nx
import json
import time
import math
import random

class DynamicLoadBalancer(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(DynamicLoadBalancer, self).__init__(*args, **kwargs)
        
        self.load_config('config.json')
        
        self.mac_to_switch = {}
        self.mac_to_port = {}
        self.datapaths = {}
        
        # ===== FIX 1: Track flows with last_reroute timestamp =====
        self.active_flows = {}  # flow_key -> {path, last_seen, last_reroute}
        
        self.topology_graph = nx.Graph()
        self.link_to_port = {}
        self.port_to_link = {}
        
        self.link_stats = {}
        self.link_utilization = {}
        self.link_capacity = 10_000_000
        
        self.congestion_threshold = 0.7
        self.reroute_benefit_threshold = 0.3
        self.flow_check_interval = 5
        self.reroute_cooldown = 15  # ← FIX 2: Wait 15s between reroutes
        self.flow_timeout = 30  # ← FIX 3: Increase timeout to 30s
        
        self.monitor_thread = hub.spawn(self._monitor_links)
        self.reroute_thread = hub.spawn(self._dynamic_rerouting)
        
        self.logger.info("="*60)
        self.logger.info("Dynamic UDP Load Balancer Started (FIXED)")
        self.logger.info("ECMP: %s", self.ecmp_enabled)
        self.logger.info("Dynamic Rerouting: ENABLED")
        self.logger.info("Congestion Threshold: %.0f%%", self.congestion_threshold * 100)
        self.logger.info("Reroute Cooldown: %ds", self.reroute_cooldown)
        self.logger.info("="*60)

    def load_config(self, config_file):
        with open(config_file) as f:
            self.config = json.load(f)
        
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
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id
        
        self.datapaths[dpid] = datapath
        if dpid not in self.mac_to_port:
            self.mac_to_port[dpid] = {}
        
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        self.logger.info("Switch s%d connected", dpid)

    @set_ev_cls(topo_event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        self.logger.info("Topology change detected, rebuilding...")
        self.discover_topology()

    @set_ev_cls(topo_event.EventLinkAdd)
    def link_add_handler(self, ev):
        link = ev.link
        src_dpid = link.src.dpid
        dst_dpid = link.dst.dpid
        src_port = link.src.port_no
        dst_port = link.dst.port_no
        
        self.link_to_port[(src_dpid, dst_dpid)] = src_port
        self.link_to_port[(dst_dpid, src_dpid)] = dst_port
        self.port_to_link[(src_dpid, src_port)] = dst_dpid
        self.port_to_link[(dst_dpid, dst_port)] = src_dpid
        
        self.link_utilization[(src_dpid, dst_dpid)] = 0.0
        self.link_utilization[(dst_dpid, src_dpid)] = 0.0
        
        self.logger.info("Link discovered: s%d port %d <-> s%d port %d",
                        src_dpid, src_port, dst_dpid, dst_port)

    def discover_topology(self):
        switches = get_switch(self)
        links = get_link(self)
        
        self.topology_graph.clear()
        self.link_to_port.clear()
        self.port_to_link.clear()
        
        for switch in switches:
            self.topology_graph.add_node(f's{switch.dp.id}')
        
        for link in links:
            src = f's{link.src.dpid}'
            dst = f's{link.dst.dpid}'
            
            weight = self.graph[src][dst]['weight'] if self.graph.has_edge(src, dst) else 1
            self.topology_graph.add_edge(src, dst, weight=weight)
            
            self.link_to_port[(link.src.dpid, link.dst.dpid)] = link.src.port_no
            self.port_to_link[(link.src.dpid, link.src.port_no)] = link.dst.dpid
        
        self.logger.info("Topology: %d switches, %d links",
                        self.topology_graph.number_of_nodes(),
                        self.topology_graph.number_of_edges())

    def _monitor_links(self):
        while True:
            for dpid in list(self.datapaths.keys()):
                self._request_stats(dpid)
            hub.sleep(2)

    def _request_stats(self, dpid):
        datapath = self.datapaths.get(dpid)
        if datapath is None:
            return
        
        parser = datapath.ofproto_parser
        req = parser.OFPPortStatsRequest(datapath, 0, datapath.ofproto.OFPP_ANY)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        
        for stat in body:
            port_no = stat.port_no
            if port_no == 0xfffffffe:
                continue
            
            if (dpid, port_no) not in self.port_to_link:
                continue
            
            neighbor_dpid = self.port_to_link[(dpid, port_no)]
            link_key = (dpid, neighbor_dpid)
            port_key = (dpid, port_no)
            
            tx_bytes = stat.tx_bytes
            current_time = time.time()
            
            if port_key in self.link_stats:
                prev_bytes = self.link_stats[port_key]['tx_bytes']
                prev_time = self.link_stats[port_key]['timestamp']
                
                delta_bytes = tx_bytes - prev_bytes
                delta_time = current_time - prev_time
                
                if delta_time > 0:
                    bandwidth_bps = (delta_bytes * 8) / delta_time
                    utilization = min(bandwidth_bps / self.link_capacity, 1.0)
                    
                    self.link_utilization[link_key] = utilization
                    
                    if utilization > 0.1:
                        self.logger.debug("Link s%d->s%d: %.1f%% (%.2f Mbps)",
                                        dpid, neighbor_dpid, utilization * 100,
                                        bandwidth_bps / 1_000_000)
            
            self.link_stats[port_key] = {
                'tx_bytes': tx_bytes,
                'timestamp': current_time
            }

    # ===== FIX 4: Improved rerouting with cooldown =====
    def _dynamic_rerouting(self):
        """Background thread to check and reroute congested flows"""
        while True:
            hub.sleep(self.flow_check_interval)
            
            flows_to_check = list(self.active_flows.items())
            
            for flow_key, flow_info in flows_to_check:
                current_time = time.time()
                
                # Check if flow is still active
                if current_time - flow_info['last_seen'] > self.flow_timeout:
                    del self.active_flows[flow_key]
                    self.logger.info("Flow expired: UDP %d->%d", flow_key[3], flow_key[4])
                    continue
                
                # ===== FIX: Check cooldown period =====
                if current_time - flow_info['last_reroute'] < self.reroute_cooldown:
                    continue  # Too soon to reroute again
                
                current_path = flow_info['path']
                current_cost = self._calculate_path_cost(current_path)
                
                # Only consider rerouting if path is congested
                if current_cost < math.exp(5 * self.congestion_threshold):
                    continue
                
                # Find alternative paths
                src_dpid, dst_dpid, dst_mac, src_port, dst_port = flow_key
                src_node = f's{src_dpid}'
                dst_node = f's{dst_dpid}'
                
                try:
                    graph_to_use = self.topology_graph if self.topology_graph.number_of_nodes() > 0 else self.graph
                    all_paths = list(nx.all_shortest_paths(
                        graph_to_use, source=src_node, target=dst_node, weight='weight'))
                    
                    best_path = None
                    best_cost = current_cost
                    
                    for alt_path in all_paths:
                        if alt_path == current_path:
                            continue
                        
                        alt_cost = self._calculate_path_cost(alt_path)
                        
                        # Only switch if significantly better
                        if alt_cost < best_cost * (1 - self.reroute_benefit_threshold):
                            best_path = alt_path
                            best_cost = alt_cost
                    
                    if best_path:
                        self.logger.warning("⚡ REROUTING UDP %d->%d", src_port, dst_port)
                        self.logger.warning("  Old: cost=%.2f %s", current_cost, current_path)
                        self.logger.warning("  New: cost=%.2f %s", best_cost, best_path)
                        
                        self._reroute_flow(flow_key, best_path)
                        flow_info['path'] = best_path
                        flow_info['last_reroute'] = current_time  # ← FIX: Update cooldown
                        
                except Exception as e:
                    self.logger.error("Rerouting error: %s", str(e))

    def _reroute_flow(self, flow_key, new_path):
        src_dpid, dst_dpid, dst_mac, udp_src, udp_dst = flow_key
        final_port = self.mac_to_port[dst_dpid][dst_mac]
        
        src_mac = None
        for mac, dpid in self.mac_to_switch.items():
            if dpid == src_dpid and mac != dst_mac:
                src_mac = mac
                break
        
        self._remove_flow_rules(flow_key)
        self.install_path_rules(new_path, dst_mac, final_port, udp_src, udp_dst,
                               src_mac=src_mac, src_port=None)

    def _remove_flow_rules(self, flow_key):
        src_dpid, dst_dpid, dst_mac, udp_src, udp_dst = flow_key
        
        for dpid in self.datapaths:
            datapath = self.datapaths[dpid]
            parser = datapath.ofproto_parser
            ofproto = datapath.ofproto
            
            match = parser.OFPMatch(
                eth_type=0x0800,
                eth_dst=dst_mac,
                ip_proto=17,
                udp_src=udp_src,
                udp_dst=udp_dst)
            
            mod = parser.OFPFlowMod(
                datapath=datapath,
                command=ofproto.OFPFC_DELETE,
                out_port=ofproto.OFPP_ANY,
                out_group=ofproto.OFPG_ANY,
                match=match)
            datapath.send_msg(mod)

    def _calculate_path_cost(self, path):
        total_cost = 0.0
        for i in range(len(path) - 1):
            src_dpid = int(path[i][1:])
            dst_dpid = int(path[i+1][1:])
            
            util = self.link_utilization.get((src_dpid, dst_dpid), 0.0)
            link_cost = math.exp(5 * util)
            total_cost += link_cost
        
        return total_cost

    def _select_path_weighted(self, paths):
        if len(paths) == 1:
            return paths[0]
        
        path_costs = [(path, self._calculate_path_cost(path)) for path in paths]
        path_weights = [(path, math.exp(-cost)) for path, cost in path_costs]
        
        total_weight = sum(w for _, w in path_weights)
        normalized = [(path, w / total_weight) for path, w in path_weights]
        
        rand = random.random()
        cumulative = 0.0
        for path, prob in normalized:
            cumulative += prob
            if rand <= cumulative:
                return path
        
        return paths[0]

    def add_flow(self, datapath, priority, match, actions):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
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
        
        if dst.startswith('33:33') or dst.startswith('01:00:5e') or dst.startswith('ff:ff'):
            return
        
        if (dpid, in_port) not in self.port_to_link:
            self.mac_to_switch[src] = dpid
            self.mac_to_port[dpid][src] = in_port
        
        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            self.flood_packet(datapath, msg, in_port)
            return
        
        if dst not in self.mac_to_switch:
            self.flood_packet(datapath, msg, in_port)
            return
        
        dst_dpid = self.mac_to_switch[dst]
        dst_port = self.mac_to_port[dst_dpid][dst]
        
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
        
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        udp_pkt = pkt.get_protocol(udp.udp)
        
        if not udp_pkt:
            self.flood_packet(datapath, msg, in_port)
            return
        
        flow_key = (dpid, dst_dpid, dst, udp_pkt.src_port, udp_pkt.dst_port)
        
        # ===== FIX 5: Always update last_seen =====
        current_time = time.time()
        
        if flow_key in self.active_flows:
            self.active_flows[flow_key]['last_seen'] = current_time  # ← FIX
        else:
            # New flow
            src_node = f's{dpid}'
            dst_node = f's{dst_dpid}'
            
            try:
                graph_to_use = self.topology_graph if self.topology_graph.number_of_nodes() > 0 else self.graph
                all_paths = list(nx.all_shortest_paths(
                    graph_to_use, source=src_node, target=dst_node, weight='weight'))
                
                if self.ecmp_enabled and len(all_paths) > 1:
                    path = self._select_path_weighted(all_paths)
                    self.logger.info("🆕 NEW UDP Flow: %d->%d via %s",
                                   udp_pkt.src_port, udp_pkt.dst_port, path)
                else:
                    path = all_paths[0]
                
                # ===== FIX: Initialize with last_reroute =====
                self.active_flows[flow_key] = {
                    'path': path,
                    'last_seen': current_time,
                    'last_reroute': current_time  # ← FIX
                }
                
                self.install_path_rules(path, dst, dst_port, udp_pkt.src_port, udp_pkt.dst_port,
                                       src_mac=src, src_port=in_port)
                
            except Exception as e:
                self.logger.error("Path computation error: %s", str(e))
                return
        
        # Forward packet
        path = self.active_flows[flow_key]['path']
        out_port = self.get_outport(dpid, path, dst_port)
        
        if out_port is None:
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

    def install_path_rules(self, path, dst_mac, final_port, udp_src, udp_dst, src_mac=None, src_port=None):
        for i, switch_name in enumerate(path):
            switch_id = int(switch_name[1:])
            if switch_id not in self.datapaths:
                continue
            
            datapath = self.datapaths[switch_id]
            parser = datapath.ofproto_parser
            
            if i < len(path) - 1:
                next_switch_id = int(path[i+1][1:])
                fwd_out_port = self.link_to_port[(switch_id, next_switch_id)]
            else:
                fwd_out_port = final_port
            
            fwd_match = parser.OFPMatch(
                eth_type=0x0800,
                eth_dst=dst_mac,
                ip_proto=17,
                udp_src=udp_src,
                udp_dst=udp_dst)
            
            fwd_actions = [parser.OFPActionOutput(fwd_out_port)]
            self.add_flow(datapath, 10, fwd_match, fwd_actions)
            
            if src_mac and src_port is not None:
                if i > 0:
                    prev_switch_id = int(path[i-1][1:])
                    rev_out_port = self.link_to_port[(switch_id, prev_switch_id)]
                else:
                    rev_out_port = src_port
                
                rev_match = parser.OFPMatch(
                    eth_type=0x0800,
                    eth_dst=src_mac,
                    ip_proto=17,
                    udp_src=udp_dst,
                    udp_dst=udp_src)
                
                rev_actions = [parser.OFPActionOutput(rev_out_port)]
                self.add_flow(datapath, 10, rev_match, rev_actions)

    def get_outport(self, switch_id, path, final_port):
        switch_name = f's{switch_id}'
        
        if switch_name not in path:
            return None
        
        idx = path.index(switch_name)
        
        if idx < len(path) - 1:
            next_id = int(path[idx+1][1:])
            return self.link_to_port.get((switch_id, next_id))
        else:
            return final_port
