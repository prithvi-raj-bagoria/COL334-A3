from mininet.topo import Topo
from mininet.net import Mininet
from mininet.log import setLogLevel, info
from mininet.cli import CLI
from mininet.node import RemoteController, OVSSwitch
# --- Step 1: Import TCLink ---
from mininet.link import TCLink

class CustomTopo(Topo):
    def build(self):
        # Set the link bandwidth to 10 Mbps
        bw = 10 

        # Add switches
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        s3 = self.addSwitch('s3')
        s4 = self.addSwitch('s4')
        s5 = self.addSwitch('s5')
        s6 = self.addSwitch('s6')

        # Add hosts
        h1 = self.addHost('h1')
        h2 = self.addHost('h2')
        
        # Connect hosts to switches (typically with no artificial limit)
        self.addLink(h1, s1)
        self.addLink(h2, s6)

        # Connect switches with each other using the specified bandwidth
        # This now works because we will tell Mininet to use TCLink
        linkopts = dict(bw=bw)
        self.addLink(s1, s2, **linkopts)
        self.addLink(s1, s3, **linkopts)
        self.addLink(s2, s4, **linkopts)
        self.addLink(s3, s5, **linkopts)
        self.addLink(s4, s6, **linkopts)
        self.addLink(s5, s6, **linkopts)
        
def run():
    """Create the network, start it, and enter the CLI."""
    topo = CustomTopo()
    # --- Step 2: Tell Mininet to use TCLink for all links ---
    net = Mininet(topo=topo, 
                  switch=OVSSwitch, 
                  build=False, 
                  controller=None,
                  link=TCLink,  # This is the crucial addition
                  autoSetMacs=True, 
                  autoStaticArp=True)
                  
    net.addController('c0', controller=RemoteController, ip="127.0.0.1", protocol='tcp', port=6633)
    net.build()
    net.start()
    info('*** Running CLI\n')
    CLI(net)


    info('*** Stopping network\n')
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    run()