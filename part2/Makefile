# Makefile for Part 2: L2-SPF Controller with ECMP

.PHONY: controller topology clean 


controller:
	@echo "Starting Ryu controller with L2-SPF..."
	ryu-manager --observe-links p2_l2spf.py

topology:
	@echo "Starting Mininet topology..."
	sudo python3 p2_topo.py

clean:
	@echo "Cleaning up..."
	sudo mn -c
	@pkill -f ryu-manager || true
	@pkill -f p2_topo.py || true
	@echo "Cleanup complete"


