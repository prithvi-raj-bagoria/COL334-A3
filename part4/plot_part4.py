#!/usr/bin/env python3
"""
Part 4: OSPF vs SDN Throughput Comparison Plot
Handles different log file naming conventions
"""

import matplotlib.pyplot as plt
import re
import sys
import os

def parse_iperf_log(filename):
    """
    Parse iperf log file and extract time and throughput data
    """
    times = []
    throughputs = []
    
    try:
        with open(filename, 'r') as f:
            for line in f:
                # Match iperf output pattern
                match = re.search(
                    r'\[\s*\d+\]\s+([\d.]+)-([\d.]+)\s+sec\s+[\d.]+\s+\w+\s+([\d.]+)\s+Mbits/sec',
                    line
                )
                if match:
                    end_time = float(match.group(2))
                    throughput = float(match.group(3))
                    times.append(end_time)
                    throughputs.append(throughput)
    
    except FileNotFoundError:
        print(f"ERROR: File '{filename}' not found!")
        return [], []
    
    return times, throughputs


def find_log_file(possible_paths):
    """Find the first existing log file from a list of possible paths"""
    for path in possible_paths:
        if os.path.exists(path):
            return path
    return None


def plot_comparison(ospf_log, sdn_log, output_file='part4_comparison.png'):
    """
    Create comparison plot of OSPF vs SDN throughput over time
    """
    
    # Parse both logs
    ospf_times, ospf_tp = parse_iperf_log(ospf_log)
    sdn_times, sdn_tp = parse_iperf_log(sdn_log)
    
    if not ospf_times or not sdn_times:
        print("ERROR: Could not parse log files!")
        print(f"\nPlease check that files exist:")
        print(f"  OSPF: {ospf_log}")
        print(f"  SDN:  {sdn_log}")
        return
    
    # Create figure
    plt.figure(figsize=(14, 7))
    
    # Plot OSPF data
    plt.plot(ospf_times, ospf_tp, 
             color='red', 
             marker='o', 
             markersize=5,
             linewidth=2, 
             label='OSPF (Traditional Routing)',
             alpha=0.8)
    
    # Plot SDN data
    plt.plot(sdn_times, sdn_tp, 
             color='blue', 
             marker='s', 
             markersize=5,
             linewidth=2, 
             label='SDN (Ryu Controller)',
             alpha=0.8)
    
    # Add vertical lines for failure events
    plt.axvline(x=2, color='gray', linestyle='--', linewidth=2, 
                label='Link Failure (t=2s)', alpha=0.7)
    plt.axvline(x=7, color='green', linestyle='--', linewidth=2, 
                label='Link Recovery (t=7s)', alpha=0.7)
    
    # Add horizontal line at 100 Mbps (fast path)
    plt.axhline(y=100, color='lightgreen', linestyle=':', linewidth=1.5, 
                label='100 Mbps (Fast Path)', alpha=0.5)
    
    # Add horizontal line at 10 Mbps (slow path)
    plt.axhline(y=10, color='orange', linestyle=':', linewidth=1.5, 
                label='10 Mbps (Slow Path)', alpha=0.5)
    
    # Labels and title
    plt.xlabel('Time (seconds)', fontsize=13, fontweight='bold')
    plt.ylabel('Throughput (Mbits/sec)', fontsize=13, fontweight='bold')
    plt.title('Part 4: OSPF vs SDN Link Failure Recovery Comparison\n' + 
              'Per-Second Throughput Analysis', 
              fontsize=15, fontweight='bold', pad=20)
    
    # Legend
    plt.legend(loc='best', fontsize=11, framealpha=0.9)
    
    # Grid
    plt.grid(True, alpha=0.3, linestyle='--', linewidth=0.7)
    
    # Set y-axis limits
    max_tp = max(max(ospf_tp), max(sdn_tp))
    plt.ylim(0, min(max_tp * 1.1, 150))  # Cap at 150 Mbps for readability
    
    # Tight layout
    plt.tight_layout()
    
    # Save figure
    plt.savefig(output_file, dpi=300, bbox_inches='tight')
    print(f"\n✅ Plot saved to: {output_file}")
    
    # Show plot
    plt.show()
    
    # Print summary statistics
    print("\n" + "="*60)
    print("SUMMARY STATISTICS")
    print("="*60)
    
    print(f"\n📊 OSPF:")
    print(f"  Average throughput: {sum(ospf_tp)/len(ospf_tp):.2f} Mbps")
    print(f"  Peak throughput: {max(ospf_tp):.2f} Mbps")
    print(f"  Minimum throughput: {min(ospf_tp):.2f} Mbps")
    
    print(f"\n📊 SDN:")
    print(f"  Average throughput: {sum(sdn_tp)/len(sdn_tp):.2f} Mbps")
    print(f"  Peak throughput: {max(sdn_tp):.2f} Mbps")
    print(f"  Minimum throughput: {min(sdn_tp):.2f} Mbps")
    
    # Calculate convergence indicators
    print(f"\n⏱️  CONVERGENCE ANALYSIS:")
    
    # Find failure convergence (throughput drop)
    ospf_fail_idx = next((i for i, tp in enumerate(ospf_tp) if i > 1 and tp < 50), None)
    sdn_fail_idx = next((i for i, tp in enumerate(sdn_tp) if i > 1 and tp < 50), None)
    
    if ospf_fail_idx:
        print(f"  OSPF failure detected at: t={ospf_times[ospf_fail_idx]:.1f}s")
        print(f"    Convergence time: {ospf_times[ospf_fail_idx] - 2:.1f}s")
    if sdn_fail_idx:
        print(f"  SDN failure detected at: t={sdn_times[sdn_fail_idx]:.1f}s")
        print(f"    Convergence time: {sdn_times[sdn_fail_idx] - 2:.1f}s")
    
    # Find recovery convergence (throughput increase)
    ospf_rec_idx = next((i for i, tp in enumerate(ospf_tp) if i > 7 and tp > 50), None)
    sdn_rec_idx = next((i for i, tp in enumerate(sdn_tp) if i > 7 and tp > 50), None)
    
    if ospf_rec_idx:
        print(f"  OSPF recovery detected at: t={ospf_times[ospf_rec_idx]:.1f}s")
        print(f"    Convergence time: {ospf_times[ospf_rec_idx] - 7:.1f}s")
    if sdn_rec_idx:
        print(f"  SDN recovery detected at: t={sdn_times[sdn_rec_idx]:.1f}s")
        print(f"    Convergence time: {sdn_times[sdn_rec_idx] - 7:.1f}s")
    
    print("="*60 + "\n")


if __name__ == '__main__':
    print("="*60)
    print("Part 4: Throughput Comparison Plot Generator")
    print("="*60)
    
    # Try to find OSPF log file
    ospf_possible_paths = [
        '/tmp/ospf_h1.log',
        'h1_iperf.log',
        '/tmp/h1_iperf.log',
        './h1_iperf.log'
    ]
    
    # Try to find SDN log file
    sdn_possible_paths = [
        '/tmp/p4_sdn_h1.log',
        '/tmp/sdn_h1.log',
        'sdn_h1_iperf.log',
        '/tmp/h1_sdn.log'
    ]
    
    # Allow command-line arguments
    if len(sys.argv) >= 3:
        ospf_log = sys.argv[1]
        sdn_log = sys.argv[2]
    else:
        ospf_log = find_log_file(ospf_possible_paths)
        sdn_log = find_log_file(sdn_possible_paths)
        
        if not ospf_log:
            print("\n❌ OSPF log file not found!")
            print("Searched in:")
            for path in ospf_possible_paths:
                print(f"  - {path}")
            print("\nPlease run OSPF test first:")
            print("  sudo python3 p4_runner.py --input-file=p4_config.json")
            sys.exit(1)
        
        if not sdn_log:
            print("\n❌ SDN log file not found!")
            print("Searched in:")
            for path in sdn_possible_paths:
                print(f"  - {path}")
            print("\nPlease run SDN test first:")
            print("  Terminal 1: ryu-manager --observe-links p4_l3spf_lf.py")
            print("  Terminal 2: sudo python3 p4_sdn_topo.py")
            print("\nOr specify log files manually:")
            print("  python3 plot_part4.py <ospf_log> <sdn_log>")
            sys.exit(1)
    
    print(f"OSPF log: {ospf_log}")
    print(f"SDN log:  {sdn_log}")
    print("="*60 + "\n")
    
    # Generate plot
    plot_comparison(ospf_log, sdn_log)
