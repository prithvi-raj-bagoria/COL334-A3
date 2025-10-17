# Makefile for Part 1 (uses p1_topo.py with RemoteController 127.0.0.1:6633)
# Run each target in a separate terminal.

RYU=ryu-manager
TOPO=sudo python3 p1_topo.py

.PHONY: p1_hub switch topo

hub:
	$(RYU) p1_hub.py

p1_learning:
	$(RYU) p1_learning.py

topo:
	$(TOPO)
