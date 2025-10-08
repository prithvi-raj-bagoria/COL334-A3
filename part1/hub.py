#!../ryu-venv/bin python3
"""
COL334 Assignment 3 - Part 1(a): Hub Controller
------------------------------------------------
Implements a hub-like controller where:
- MAC address table is maintained ONLY at the controller (not on switches)
- Every packet triggers a PACKET_IN message to the controller
- Controller handles each packet individually using PACKET_OUT
- NO flow rules are installed on switches (except table-miss)
- Results in lower throughput due to control plane bottleneck

"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet


class HubController(app_manager.RyuApp):
    """
    Hub Controller that handles all packets at the controller level.
    No per-destination flow rules are installed on switches.
    """
    
    # Specify OpenFlow version 1.3
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        """
        Initialize the hub controller.
        Creates a MAC address learning table stored at the controller.
        """
        super(HubController, self).__init__(*args, **kwargs)
        
        # MAC address learning table: {switch_id: {mac_address: port}}
        # Example: {1: {'00:00:00:00:00:01': 1, '00:00:00:00:00:02': 2}}
        # This table exists ONLY in controller memory, NOT on switches
        self.mac_to_port = {}

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """
        Handle switch connection event.
        Called when a switch connects to the controller.
        Installs the table-miss flow entry that sends all packets to controller.
        
        Args:
            ev: Event object containing switch information
        """
        # Extract switch (datapath) object and protocol details
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto       # OpenFlow protocol constants
        parser = datapath.ofproto_parser  # Parser for creating OpenFlow messages
        
        # self.logger.info("Switch %s connected", datapath.id)

        # Create table-miss flow entry
        # This is the ONLY flow rule installed by hub controller
        match = parser.OFPMatch()  # Empty match = match ALL packets
        
        # Action: Send packet to controller with full packet data
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        
        # Wrap actions in instructions
        instructions = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                                     actions)]
        
        # Create flow modification message
        # Priority 0 = lowest priority (only matches if no other rule matches)
        mod = parser.OFPFlowMod(datapath=datapath,
                                priority=0,
                                match=match,
                                instructions=instructions)
        
        # Send flow mod message to switch to install the rule
        datapath.send_msg(mod)
        
        # self.logger.info("Table-miss flow installed on switch %s", datapath.id)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """
        Handle PACKET_IN events from switches.
        This is called for EVERY packet because only table-miss rule exists.
        
        Processing steps:
        1. Parse packet to extract source and destination MAC
        2. Learn source MAC to port mapping (store in controller table)
        3. Look up destination MAC in controller table
        4. If found: forward to specific port
        5. If not found: flood to all ports
        6. Send PACKET_OUT message (NO flow rule installation)
        
        Args:
            ev: Event object containing packet information
        """
        # Extract message and switch information
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']  # Port where packet arrived
        dpid = datapath.id               # Switch ID (datapath ID)

        # Parse the packet data
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        
        # Ignore non-Ethernet packets (safety check)
        if eth is None:
            return
        
        # Extract source and destination MAC addresses
        src_mac = eth.src  # Source MAC (e.g., '00:00:00:00:00:01')
        dst_mac = eth.dst  # Destination MAC (e.g., '00:00:00:00:00:03')

        # Log packet information
        # self.logger.info("Packet in: switch=%s, src=%s, dst=%s, in_port=%s",
        #                 dpid, src_mac, dst_mac, in_port)

        # ====================
        # MAC Learning Phase
        # ====================
        # Learn the source MAC address and associate it with input port
        # If switch not in table, create empty dict for it
        self.mac_to_port.setdefault(dpid, {})
        
        # Store/update the MAC-to-port mapping in controller's table
        self.mac_to_port[dpid][src_mac] = in_port
        # self.logger.debug("Learned: switch=%s, MAC=%s -> port=%s",
        #                  dpid, src_mac, in_port)

        # ====================
        # Forwarding Decision
        # ====================
        # Look up destination MAC in controller's table
        if dst_mac in self.mac_to_port[dpid]:
            # Destination is known: forward to specific port
            out_port = self.mac_to_port[dpid][dst_mac]
            # self.logger.debug("Destination known: forwarding to port %s", out_port)
        else:
            # Destination unknown: flood to all ports (except input port)
            out_port = ofproto.OFPP_FLOOD
            # self.logger.debug("Destination unknown: flooding packet")

        # Create output action
        actions = [parser.OFPActionOutput(out_port)]

        # ====================
        # Send Packet Out
        # ====================
        # CRITICAL: Hub controller only sends PACKET_OUT, NO flow rule installation
        # This is the key difference from learning switch
        
        # Handle buffered vs. unbuffered packets
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            # Packet not buffered at switch, must send full packet data
            data = msg.data

        # Create and send PACKET_OUT message
        out = parser.OFPPacketOut(datapath=datapath,
                                  buffer_id=msg.buffer_id,
                                  in_port=in_port,
                                  actions=actions,
                                  data=data)
        datapath.send_msg(out)
        
        # self.logger.debug("Packet forwarded via PACKET_OUT")
        
        # NOTE: Next packet will also trigger PACKET_IN because
        # no flow rule was installed on the switch!
