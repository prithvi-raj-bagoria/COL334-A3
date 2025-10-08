# Makefile for COL334 Assignment 3 - Part 2
# L2 Shortest Path Routing with/without ECMP

# Variables
PYTHON=python3
RYU=ryu-manager
TOPO=sudo python3 p2_topo.py
VENV=source ../ryu-venv/bin/activate
CONTROLLER=p2_l2psf.py
CONFIG=config.json

.PHONY: shortest-path ecmp topo clean test-sp test-ecmp help

# Default target
help:
	@echo "COL334 Assignment 3 - Part 2 Targets:"
	@echo ""
	@echo "  make shortest-path  - Run shortest path controller (Terminal 1)"
	@echo "  make topo          - Run mininet topology (Terminal 2)"
	@echo "  make clean         - Clean mininet state"
	@echo "  make test-sp       - Show test commands for shortest path"
	@echo "  make test-ecmp     - Show test commands for ECMP"
	@echo "  make enable-ecmp   - Enable ECMP in config.json"
	@echo "  make disable-ecmp  - Disable ECMP in config.json"
	@echo ""
	@echo "Workflow:"
	@echo "  Terminal 1: make shortest-path"
	@echo "  Terminal 2: make topo"
	@echo "  In Mininet: Run test commands"

# Run shortest path controller (needs virtual environment)
shortest-path:
	@echo "Starting Shortest Path Controller..."
	@echo "Make sure virtual environment is activated!"
	@echo "Reading topology from $(CONFIG)..."
	$(RYU) --verbose $(CONTROLLER)

# Alias for consistency
controller: shortest-path

# Run mininet topology
topo:
	@echo "Starting Mininet topology..."
	$(TOPO)

# Clean mininet state
clean:
	@echo "Cleaning Mininet..."
	sudo mn -c

# Enable ECMP in config
enable-ecmp:
	@echo "Enabling ECMP in $(CONFIG)..."
	@python3 -c "import json; \
		data = json.load(open('$(CONFIG)')); \
		data['ecmp'] = True; \
		json.dump(data, open('$(CONFIG)', 'w'), indent=2)"
	@echo "ECMP enabled. Restart controller to apply changes."

# Disable ECMP in config
disable-ecmp:
	@echo "Disabling ECMP in $(CONFIG)..."
	@python3 -c "import json; \
		data = json.load(open('$(CONFIG)')); \
		data['ecmp'] = False; \
		json.dump(data, open('$(CONFIG)', 'w'), indent=2)"
	@echo "ECMP disabled. Restart controller to apply changes."

# Show test commands for shortest path mode
test-sp:
	@echo ""
	@echo "=== Test Commands for Shortest Path Mode (ECMP=false) ==="
	@echo ""
	@echo "1. Verify connectivity:"
	@echo "   mininet> pingall"
	@echo ""
	@echo "2. Test throughput h1 to h2:"
	@echo "   mininet> iperf h1 h2"
	@echo ""
	@echo "3. Check installed flow rules on all switches:"
	@echo "   mininet> sh ovs-ofctl dump-flows s1"
	@echo "   mininet> sh ovs-ofctl dump-flows s2"
	@echo "   mininet> sh ovs-ofctl dump-flows s3"
	@echo "   mininet> sh ovs-ofctl dump-flows s4"
	@echo "   mininet> sh ovs-ofctl dump-flows s5"
	@echo "   mininet> sh ovs-ofctl dump-flows s6"
	@echo ""
	@echo "4. Trace path using traceroute:"
	@echo "   mininet> h1 traceroute -n h2"
	@echo ""
	@echo "5. Check link utilization:"
	@echo "   mininet> links"
	@echo ""
	@echo "6. View network topology:"
	@echo "   mininet> net"
	@echo ""

# Show test commands for ECMP mode
test-ecmp:
	@echo ""
	@echo "=== Test Commands for ECMP Mode (ECMP=true) ==="
	@echo ""
	@echo "1. Verify connectivity:"
	@echo "   mininet> pingall"
	@echo ""
	@echo "2. Test throughput multiple times (should vary):"
	@echo "   mininet> iperf h1 h2"
	@echo "   mininet> iperf h1 h2"
	@echo "   mininet> iperf h1 h2"
	@echo ""
	@echo "3. Clear flows and test again:"
	@echo "   mininet> sh ovs-ofctl del-flows s1"
	@echo "   mininet> sh ovs-ofctl del-flows s2"
	@echo "   mininet> sh ovs-ofctl del-flows s3"
	@echo "   mininet> sh ovs-ofctl del-flows s4"
	@echo "   mininet> sh ovs-ofctl del-flows s5"
	@echo "   mininet> sh ovs-ofctl del-flows s6"
	@echo "   mininet> iperf h1 h2"
	@echo ""
	@echo "4. Check which path was chosen:"
	@echo "   mininet> sh ovs-ofctl dump-flows s1"
	@echo "   (Look at output port - should vary between runs)"
	@echo ""
