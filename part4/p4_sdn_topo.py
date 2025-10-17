#!/usr/bin/env python3
"""
Part 4 SDN Topology - Updated for 100 Mbps vs 10 Mbps Path Differentiation
Adapted from professor's updated OSPF topology
Path 1 (s1->s2->s3->s6): 100 Mbps
Path 2 (s1->s4->s5->s6): 10 Mbps (bottleneck at s5-s4)
"""

from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info
import time


def hex_dpid(n: int) -> str:
    """Convert switch number to hex DPID"""
    return f"{int(n):016x}"


def set_if(node, ifname, ip_cidr=None, mac=None):
    """Configure interface with IP and MAC"""
    node.cmd(f'ip link set dev {ifname} down')
    node.cmd(f'ip addr flush dev {ifname}')
    if mac:
        node.cmd(f'ip link set dev {ifname} address {mac}')
    if ip_cidr:
        node.cmd(f'ip addr add {ip_cidr} dev {ifname}')
    node.cmd(f'ip link set {ifname} up')


def build_and_test():
    """Build topology and run automated link failure test"""
    
    net = Mininet(
        controller=None, 
        build=False, 
        link=TCLink,
        autoSetMacs=False, 
        autoStaticArp=False
    )

    # Add SDN controller
    info('*** Adding controller\n')
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6633)

    # Add switches (OVS with OpenFlow 1.3)
    info('*** Adding switches\n')
    s1 = net.addSwitch('s1', cls=OVSSwitch, dpid=hex_dpid(1), protocols='OpenFlow13')
    s2 = net.addSwitch('s2', cls=OVSSwitch, dpid=hex_dpid(2), protocols='OpenFlow13')
    s3 = net.addSwitch('s3', cls=OVSSwitch, dpid=hex_dpid(3), protocols='OpenFlow13')
    s4 = net.addSwitch('s4', cls=OVSSwitch, dpid=hex_dpid(4), protocols='OpenFlow13')
    s5 = net.addSwitch('s5', cls=OVSSwitch, dpid=hex_dpid(5), protocols='OpenFlow13')
    s6 = net.addSwitch('s6', cls=OVSSwitch, dpid=hex_dpid(6), protocols='OpenFlow13')

    # Add hosts
    info('*** Adding hosts\n')
    h1 = net.addHost('h1', ip='10.0.12.2/24', mac='00:00:00:00:01:02')
    h2 = net.addHost('h2', ip='10.0.67.2/24', mac='00:00:00:00:06:02')

    # Host <-> switch links with 100 Mbps (matching OSPF topology)
    info('*** Adding host links with 100 Mbps bandwidth\n')
    net.addLink(h1, s1, intfName1='h1-eth1', intfName2='s1-eth1', 
                port1=1, port2=1, 
                cls=TCLink, bw=100, delay='1ms', max_queue_size=1000)
    net.addLink(h2, s6, intfName1='h2-eth1', intfName2='s6-eth3', 
                port1=1, port2=3, 
                cls=TCLink, bw=100, delay='1ms', max_queue_size=1000)

    # Inter-switch links with differentiated bandwidth
    info('*** Adding inter-switch links\n')
    
    # s1 <-> s2: No explicit limit (default speed)
    net.addLink(s1, s2, intfName1='s1-eth2', intfName2='s2-eth1', 
                port1=2, port2=1, 
                cls=TCLink, delay='1ms', max_queue_size=1000)
    
    # s2 <-> s3: 100 Mbps (FAST PATH)
    net.addLink(s2, s3, intfName1='s2-eth2', intfName2='s3-eth1', 
                port1=2, port2=1, 
                cls=TCLink, bw=100, delay='1ms', max_queue_size=1000)
    
    # s3 <-> s6: No explicit limit (default speed)
    net.addLink(s3, s6, intfName1='s3-eth2', intfName2='s6-eth1', 
                port1=2, port2=1, 
                cls=TCLink, delay='1ms', max_queue_size=1000)
    
    # s6 <-> s5: No explicit limit (default speed)
    net.addLink(s6, s5, intfName1='s6-eth2', intfName2='s5-eth2', 
                port1=2, port2=2, 
                cls=TCLink, delay='1ms', max_queue_size=1000)
    
    # s5 <-> s4: 10 Mbps (SLOW PATH BOTTLENECK)
    net.addLink(s5, s4, intfName1='s5-eth1', intfName2='s4-eth2', 
                port1=1, port2=2, 
                cls=TCLink, bw=10, delay='1ms', max_queue_size=1000)
    
    # s4 <-> s1: No explicit limit (default speed)
    net.addLink(s4, s1, intfName1='s4-eth1', intfName2='s1-eth3', 
                port1=1, port2=3, 
                cls=TCLink, delay='1ms', max_queue_size=1000)

    # Build and start network
    info('*** Building network\n')
    net.build()
    net.start()

    # Configure switch interfaces with gateway IPs/MACs
    info('*** Configuring switch interfaces\n')
    
    # Gateway interfaces for hosts
    set_if(s1, 's1-eth1', ip_cidr='10.0.12.1/24', mac='00:00:00:00:01:01')
    set_if(s6, 's6-eth3', ip_cidr='10.0.67.1/24', mac='00:00:00:00:06:03')

    # Inter-switch links
    # s1 <-> s2 (10.0.13.0/24)
    set_if(s1, 's1-eth2', ip_cidr='10.0.13.1/24', mac='00:00:00:00:01:04')
    set_if(s2, 's2-eth1', ip_cidr='10.0.13.2/24', mac='00:00:00:00:02:01')

    # s2 <-> s3 (10.0.23.0/24) - 100 Mbps link
    set_if(s2, 's2-eth2', ip_cidr='10.0.23.1/24', mac='00:00:00:00:02:02')
    set_if(s3, 's3-eth1', ip_cidr='10.0.23.2/24', mac='00:00:00:00:03:01')

    # s3 <-> s6 (10.0.36.0/24)
    set_if(s3, 's3-eth2', ip_cidr='10.0.36.1/24', mac='00:00:00:00:03:02')
    set_if(s6, 's6-eth1', ip_cidr='10.0.36.2/24', mac='00:00:00:00:06:01')

    # s6 <-> s5 (10.0.56.0/24)
    set_if(s6, 's6-eth2', ip_cidr='10.0.56.2/24', mac='00:00:00:00:06:04')
    set_if(s5, 's5-eth2', ip_cidr='10.0.56.1/24', mac='00:00:00:00:05:02')

    # s5 <-> s4 (10.0.45.0/24) - 10 Mbps link (BOTTLENECK)
    set_if(s5, 's5-eth1', ip_cidr='10.0.45.2/24', mac='00:00:00:00:05:01')
    set_if(s4, 's4-eth2', ip_cidr='10.0.45.1/24', mac='00:00:00:00:04:02')

    # s4 <-> s1 (10.0.14.0/24)
    set_if(s4, 's4-eth1', ip_cidr='10.0.14.2/24', mac='00:00:00:00:04:01')
    set_if(s1, 's1-eth3', ip_cidr='10.0.14.1/24', mac='00:00:00:00:01:03')

    # Configure hosts
    info('*** Configuring hosts\n')
    h1.cmd('ip addr flush dev h1-eth1')
    h1.cmd('ip addr add 10.0.12.2/24 dev h1-eth1')
    h1.cmd('ip link set h1-eth1 address 00:00:00:00:01:02 up')
    h1.cmd('ip route add default via 10.0.12.1 dev h1-eth1')

    h2.cmd('ip addr flush dev h2-eth1')
    h2.cmd('ip addr add 10.0.67.2/24 dev h2-eth1')
    h2.cmd('ip link set h2-eth1 address 00:00:00:00:06:02 up')
    h2.cmd('ip route add default via 10.0.67.1 dev h2-eth1')

    # Wait for controller discovery
    info('*** Waiting 15 seconds for controller topology discovery\n')
    time.sleep(15)

    # Test connectivity
    info('*** Testing connectivity\n')
    result = h1.cmd('ping -c 3 10.0.67.2')
    info(result)
    
    if '3 received' not in result:
        info('*** WARNING: Ping failed! Check controller.\n')
        CLI(net)
        net.stop()
        return

    # Start automated link failure test
    info('\n' + '='*70 + '\n')
    info('*** Starting Link Failure Experiment\n')
    info('*** Path 1 (s1->s2->s3->s6): 100 Mbps\n')
    info('*** Path 2 (s1->s4->s5->s6): 10 Mbps (bottleneck at s5-s4)\n')
    info('='*70 + '\n')
    
    info('*** Starting iperf server on h2\n')
    h2.cmd('iperf -s > /tmp/p4_sdn_h2.log 2>&1 &')
    time.sleep(2)
    
    # Start iperf client with per-second reporting (30 seconds total)
    info('*** Starting iperf client on h1 (30 seconds with per-second stats)\n')
    h1.sendCmd('iperf -c 10.0.67.2 -t 30 -i 1 | tee /tmp/p4_sdn_h1.log')
    
    # Wait 2 seconds for OSPF stabilization, then fail link at t=2s
    time.sleep(2)
    
    info('\n' + '='*70 + '\n')
    info('*** FAILING LINK s2-s3 (at t=2s)\n')
    info('*** Expected: Traffic should reroute to 10 Mbps path\n')
    info('='*70 + '\n')
    s2.cmd('ip link set s2-eth2 down')
    s3.cmd('ip link set s3-eth1 down')
    
    # Wait 5 seconds with failed link, then restore at t=7s
    time.sleep(5)
    
    info('\n' + '='*70 + '\n')
    info('*** RESTORING LINK s2-s3 (at t=7s)\n')
    info('*** Expected: Traffic should return to 100 Mbps path\n')
    info('='*70 + '\n')
    s2.cmd('ip link set s2-eth2 up')
    s3.cmd('ip link set s3-eth1 up')
    
    # Wait for iperf to complete (30 - 7 = 23 more seconds)
    h1.waitOutput()
    
    # Display results
    info('\n' + '='*70 + '\n')
    info('*** Results:\n')
    info('='*70 + '\n')
    result = h1.cmd('cat /tmp/p4_sdn_h1.log')
    info(result)
    
    info('\n*** Logs saved to:\n')
    info('  - /tmp/p4_sdn_h1.log (iperf client output)\n')
    info('  - /tmp/p4_sdn_h2.log (iperf server output)\n')
    info('*** Check controller logs for convergence times\n')
    
    # Display link statistics
    info('\n*** Link Statistics:\n')
    info('  - Path 1 (s1->s2->s3->s6): 100 Mbps (fast path)\n')
    info('  - Path 2 (s1->s4->s5->s6): 10 Mbps (slow path, bottleneck at s5-s4)\n')
    info('  - Expected throughput before failure: ~95 Mbps (TCP overhead)\n')
    info('  - Expected throughput after failure: ~9.5 Mbps (10 Mbps link limit)\n')
    info('  - Convergence time: Should be visible as throughput drop/recovery\n')
    
    # Optional: Drop into CLI for manual testing
    info('\n*** Starting CLI (type "exit" to quit)\n')
    CLI(net)
    
    # Cleanup
    info('*** Stopping network\n')
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    build_and_test()
