#!/usr/bin/env python3
"""
COL334 Assignment 3 - Part 2: Layer-2 Shortest Path Routing with ECMP
-----------------------------------------------------------------------
Implements shortest-path routing using Dijkstra's algorithm while performing
L2-like forwarding (single subnet). Supports ECMP (Equal-Cost Multi-Path)
load balancing when enabled.

Key Features:
- Reads topology from config.json (nodes, weight matrix)
- Computes shortest paths using NetworkX's Dijkstra implementation
- Optionally load-balances across equal-cost paths (ECMP)
- Installs flow rules along computed paths

"""

import json
import random
import networkx as nx

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet
from ryu.topology import event as topo_event
from ryu.topology.api import get_switch, get_link


class L2ShortestPath(app_manager.RyuApp):
    """
    L2 Shortest Path Routing Controller with optional ECMP support.
    Routes packets along shortest paths in a weighted topology.
    """
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(L2ShortestPath, self).__init__(*args, **kwargs)
        
        # MAC address learning: {dpid: {mac: port}}
        self.mac_to_port = {}
        
        # Network topology as weighted graph
        self.topology = nx.Graph()
        
        # ECMP flag: enable equal-cost multipath load balancing
        self.ecmp = False
        
        # Switch-to-switch port mapping: {(src_dpid, dst_dpid): src_port}
        # Example: {('s1', 's2'): 3} means s1's port 3 connects to s2
        self.adjacency = {}
        
        # Load topology from configuration file
        self._load_topology()

    def _load_topology(self):
        """
        Load network topology from config.json file.
        
        Expected JSON format:
        {
            "ecmp": true/false,
            "nodes": ["s1", "s2", "s3", ...],
            "weight_matrix": [
                [0, 10, 20, ...],
                [10, 0, 15, ...],
                ...
            ]
        }
        
        weight_matrix[i][j] represents link cost between nodes[i] and nodes[j].
        A value of 0 means no direct link.
        """
        try:
            with open("config.json") as f:
                conf = json.load(f)
        except FileNotFoundError:
            self.logger.error("config.json not found! Please provide topology file.")
            return
        
        # Read ECMP configuration
        self.ecmp = conf.get("ecmp", False)
        self.logger.info("ECMP mode: %s", "ENABLED" if self.ecmp else "DISABLED")
        
        # Read nodes and weight matrix
        nodes = conf["nodes"]
        matrix = conf["weight_matrix"]

        # Build weighted graph from adjacency matrix
        for i, src_node in enumerate(nodes):
            self.topology.add_node(src_node)
            for j, dst_node in enumerate(nodes):
                weight = matrix[i][j]
                if weight > 0:  # Link exists
                    self.topology.add_edge(src_node, dst_node, weight=weight)
                    self.logger.debug("Added link: %s <-> %s (weight=%d)",
                                    src_node, dst_node, weight)

    @set_ev_cls(topo_event.EventSwitchEnter)
    def get_topology_data(self, ev):
        """
        Handle switch discovery event.
        Learn switch-to-switch connections and port mappings.
        
        This is crucial for determining which port to use when forwarding
        packets along a computed path.
        """
        # Get all links in the network
        link_list = get_link(self)
        
        for link in link_list:
            src_switch = f"s{link.src.dpid}"
            dst_switch = f"s{link.dst.dpid}"
            src_port = link.src.port_no
            dst_port = link.dst.port_no
            
            # Store bidirectional adjacency information
            self.adjacency[(src_switch, dst_switch)] = src_port
            self.adjacency[(dst_switch, src_switch)] = dst_port
            
            self.logger.debug("Link discovered: %s port %d <-> %s port %d",
                            src_switch, src_port, dst_switch, dst_port)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """
        Handle switch connection event.
        Install table-miss flow entry to send unknown packets to controller.
        
        Args:
            ev: Event containing switch connection information
        """
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        self.logger.info("Switch s%d connected", dp.id)

        # Install table-miss flow: send unmatched packets to controller
        match = parser.OFPMatch()  # Match all packets
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                          ofp.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=dp, priority=0,
                                match=match, instructions=inst)
        dp.send_msg(mod)

    def install_path(self, path, dst_mac, first_port, last_port, buffer_id, data):
        """
        Install flow rules along the computed shortest path.
        
        Args:
            path: List of switches representing the route (e.g., ['s1', 's3', 's6'])
            dst_mac: Destination MAC address
            first_port: Input port at first switch (where packet arrived)
            last_port: Output port at last switch (where destination host is)
            buffer_id: Buffer ID for first packet (if buffered)
            data: Packet data (if not buffered)
        """
        # Install rules on intermediate switches
        for i in range(len(path) - 1):
            current_switch = path[i]
            next_switch = path[i + 1]
            
            # Get the port connecting current_switch to next_switch
            out_port = self.adjacency.get((current_switch, next_switch))
            
            if out_port is None:
                self.logger.error("No port found from %s to %s", current_switch, next_switch)
                continue
            
            # Get datapath object for current switch
            dpid = int(current_switch[1:])  # 's1' -> 1
            datapath = get_switch(self, dpid)[0].dp
            parser = datapath.ofproto_parser
            ofp = datapath.ofproto
            
            # Create match and action
            match = parser.OFPMatch(eth_dst=dst_mac)
            actions = [parser.OFPActionOutput(out_port)]
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            
            # Install flow with idle timeout
            mod = parser.OFPFlowMod(datapath=datapath,
                                    priority=1,
                                    match=match,
                                    instructions=inst,
                                    idle_timeout=60,
                                    hard_timeout=0)
            datapath.send_msg(mod)
            
            self.logger.info("Flow installed on %s: eth_dst=%s -> port %d",
                           current_switch, dst_mac, out_port)
        
        # Send the first packet out on the first switch
        first_dpid = int(path[0][1:])
        first_dp = get_switch(self, first_dpid)[0].dp
        parser = first_dp.ofproto_parser
        ofp = first_dp.ofproto
        
        # Determine output port for first switch
        if len(path) > 1:
            out_port = self.adjacency.get((path[0], path[1]))
        else:
            out_port = last_port  # Destination on same switch
        
        actions = [parser.OFPActionOutput(out_port)]
        
        # Send packet out
        if buffer_id != ofp.OFP_NO_BUFFER:
            out = parser.OFPPacketOut(datapath=first_dp,
                                      buffer_id=buffer_id,
                                      in_port=first_port,
                                      actions=actions)
        else:
            out = parser.OFPPacketOut(datapath=first_dp,
                                      buffer_id=ofp.OFP_NO_BUFFER,
                                      in_port=first_port,
                                      actions=actions,
                                      data=data)
        first_dp.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        """
        Handle PACKET_IN events from switches.
        
        Processing steps:
        1. Learn source MAC to port mapping
        2. If destination known: compute shortest path and install flows
        3. If destination unknown: flood packet
        
        Args:
            ev: Event containing packet information
        """
        msg = ev.msg
        dp = msg.datapath
        dpid = f"s{dp.id}"
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        in_port = msg.match['in_port']

        # Parse packet
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        
        if eth is None:
            return
        
        dst_mac = eth.dst
        src_mac = eth.src

        self.logger.info("Packet in: switch=%s, src=%s, dst=%s, in_port=%d",
                        dpid, src_mac, dst_mac, in_port)

        # Learn source MAC address
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src_mac] = in_port
        self.logger.debug("Learned: %s -> %s port %d", src_mac, dpid, in_port)

        # Find destination switch and port
        dst_switch = None
        dst_port = None
        
        for switch, mac_table in self.mac_to_port.items():
            if dst_mac in mac_table:
                dst_switch = switch
                dst_port = mac_table[dst_mac]
                break

        # If destination is unknown, flood the packet
        if dst_switch is None:
            self.logger.debug("Destination %s unknown, flooding", dst_mac)
            actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
            out = parser.OFPPacketOut(datapath=dp,
                                      buffer_id=msg.buffer_id,
                                      in_port=in_port,
                                      actions=actions,
                                      data=msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None)
            dp.send_msg(out)
            return

        # Compute shortest path from source to destination switch
        try:
            if self.ecmp:
                # ECMP enabled: randomly select among equal-cost paths
                all_paths = list(nx.all_shortest_paths(self.topology,
                                                       source=dpid,
                                                       target=dst_switch,
                                                       weight='weight'))
                path = random.choice(all_paths)
                self.logger.info("ECMP: Selected path %s from %d options",
                               path, len(all_paths))
            else:
                # Single shortest path
                path = nx.shortest_path(self.topology,
                                       source=dpid,
                                       target=dst_switch,
                                       weight='weight')
                self.logger.info("Shortest path: %s", path)
            
            # Install flows along the path
            self.install_path(path, dst_mac, in_port, dst_port,
                            msg.buffer_id, msg.data)
            
        except nx.NetworkXNoPath:
            self.logger.error("No path found from %s to %s", dpid, dst_switch)
            # Flood if no path exists
            actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
            out = parser.OFPPacketOut(datapath=dp,
                                      buffer_id=msg.buffer_id,
                                      in_port=in_port,
                                      actions=actions,
                                      data=msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None)
            dp.send_msg(out)
