#!/usr/bin/env python3
"""
COL334 Assignment 3 - Part 1(b): Learning Switch
------------------------------------------------
Implements a self-learning switch where:
- MAC address learning occurs at the controller initially
- Flow rules are installed on switches for known destinations
- Switches forward packets autonomously after learning
- Results in higher throughput due to data plane forwarding

Author: [Your Name]
Date: October 2025
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet


class LearningSwitch(app_manager.RyuApp):
    """
    Learning Switch that installs flow rules on switches.
    After initial learning, switches forward packets independently.
    """
    
    # Specify OpenFlow version 1.3
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        """
        Initialize the learning switch controller.
        Creates a MAC address learning table.
        """
        super(LearningSwitch, self).__init__(*args, **kwargs)
        
        # MAC address learning table: {switch_id: {mac_address: port}}
        self.mac_to_port = {}

    def add_flow(self, datapath, priority, match, actions, buffer_id=None):
        """
        Install a flow rule on a switch.
        This is a helper function to create and send flow modification messages.
        
        Args:
            datapath: Switch to install the flow on
            priority: Rule priority (higher = checked first)
            match: Match conditions (e.g., destination MAC)
            actions: Actions to perform when matched
            buffer_id: ID of buffered packet (if any)
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Wrap actions in instructions
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]

        # Create flow mod message
        if buffer_id:
            # If packet is buffered, reference it
            mod = parser.OFPFlowMod(datapath=datapath,
                                    buffer_id=buffer_id,
                                    priority=priority,
                                    match=match,
                                    instructions=inst)
        else:
            # No buffered packet
            mod = parser.OFPFlowMod(datapath=datapath,
                                    priority=priority,
                                    match=match,
                                    instructions=inst)
        
        # Send flow mod to switch
        datapath.send_msg(mod)
        
        # self.logger.debug("Flow installed on switch %s: match=%s, actions=%s, priority=%s",
        #                  datapath.id, match, actions, priority)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """
        Handle switch connection event.
        Installs table-miss flow entry for unknown packets.
        
        Args:
            ev: Event object containing switch information
        """
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # self.logger.info("Switch %s connected", datapath.id)

        # Install table-miss flow: send unknown packets to controller
        match = parser.OFPMatch()  # Match all packets
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        
        # Priority 0 = lowest priority (default rule)
        self.add_flow(datapath, 0, match, actions)
        
        # self.logger.info("Table-miss flow installed on switch %s", datapath.id)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """
        Handle PACKET_IN events from switches.
        Only called for UNKNOWN flows (after that, switch handles forwarding).
        
        Processing steps:
        1. Parse packet to extract source and destination MAC
        2. Learn source MAC to port mapping
        3. Look up destination MAC
        4. If found: install flow rule on switch AND forward packet
        5. If not found: flood packet (no flow rule)
        
        Args:
            ev: Event object containing packet information
        """
        # Extract message and switch information
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']
        dpid = datapath.id

        # Parse the packet
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        
        # Ignore non-Ethernet packets
        if eth is None:
            return
        
        src_mac = eth.src
        dst_mac = eth.dst

        # self.logger.info("Packet in: switch=%s, src=%s, dst=%s, in_port=%s",
        #                 dpid, src_mac, dst_mac, in_port)

        # ====================
        # MAC Learning Phase
        # ====================
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src_mac] = in_port
        # self.logger.debug("Learned: switch=%s, MAC=%s -> port=%s",
        #                  dpid, src_mac, in_port)

        # ====================
        # Forwarding Decision
        # ====================
        if dst_mac in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst_mac]
            # self.logger.debug("Destination known: will install flow to port %s", out_port)
        else:
            out_port = ofproto.OFPP_FLOOD
            # self.logger.debug("Destination unknown: will flood packet")

        # Create output action
        actions = [parser.OFPActionOutput(out_port)]

        # ====================
        # Install Flow Rule (KEY DIFFERENCE FROM HUB)
        # ====================
        # If destination is known (not flooding), install a flow rule on switch
        if out_port != ofproto.OFPP_FLOOD:
            # Create match condition: packets with this destination MAC
            match = parser.OFPMatch(eth_dst=dst_mac)
            
            # Install flow rule with priority 1 (higher than table-miss)
            # Future packets matching this flow will be handled by switch directly
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                # If packet is buffered at switch, install flow and we're done
                self.add_flow(datapath, 1, match, actions, msg.buffer_id)
                # self.logger.info("Flow installed: switch=%s, dst=%s -> port=%s",
                #                dpid, dst_mac, out_port)
                return  # No need to send packet_out, switch will forward buffered packet
            else:
                # Packet not buffered, install flow and then send packet_out below
                self.add_flow(datapath, 1, match, actions)
                # self.logger.info("Flow installed: switch=%s, dst=%s -> port=%s",
                #                dpid, dst_mac, out_port)

        # ====================
        # Send Current Packet
        # ====================
        # Send PACKET_OUT for the current packet
        # (subsequent packets will match installed flow and bypass controller)
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(datapath=datapath,
                                  buffer_id=msg.buffer_id,
                                  in_port=in_port,
                                  actions=actions,
                                  data=data)
        datapath.send_msg(out)
        
        # self.logger.debug("Current packet forwarded via PACKET_OUT")
