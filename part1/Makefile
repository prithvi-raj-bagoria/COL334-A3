# Makefile for Part 1 (uses p1_topo.py with RemoteController 127.0.0.1:6633)
# Run each target in a separate terminal.

RYU=ryu-manager
TOPO=sudo python3 p1_topo.py

.PHONY: hub switch topo

hub:
	$(RYU) hub.py

switch:
	$(RYU) switch.py

topo:
	$(TOPO)
