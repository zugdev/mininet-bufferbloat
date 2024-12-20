from mininet.topo import Topo
from mininet.node import CPULimitedHost
from mininet.link import TCLink
from mininet.net import Mininet
from mininet.log import lg, info
from mininet.util import dumpNodeConnections
from mininet.cli import CLI
from mininet.clean import cleanup

from subprocess import Popen, PIPE
from time import sleep, time
from multiprocessing import Process
from argparse import ArgumentParser

from monitor import monitor_qlen

import sys
import os
import math
import random

parser = ArgumentParser(description="messages_bufferbloat tests with TCP small message scenario")
parser.add_argument('--bw-host', '-B',
                    type=float,
                    help="Bandwidth of host links (Mb/s)",
                    default=1000)

parser.add_argument('--bw-net', '-b',
                    type=float,
                    help="Bandwidth of bottleneck (network) link (Mb/s)",
                    required=True)

parser.add_argument('--delay',
                    type=float,
                    help="Link propagation delay (ms)",
                    required=True)

parser.add_argument('--dir', '-d',
                    help="Directory to store outputs",
                    required=True)

parser.add_argument('--time', '-t',
                    help="Duration (sec) to run the experiment",
                    type=int,
                    default=10)

parser.add_argument('--maxq',
                    type=int,
                    help="Max buffer size of network interface in packets",
                    default=100)

parser.add_argument('--cong',
                    help="Congestion control algorithm to use",
                    default="reno")

args = parser.parse_args()

class BBTopo(Topo):
    """
    Simple topology for the TCP small message experiment.
    h1 <-> s0 <-> h2
    """
    def build(self, n=2):
        h1 = self.addHost('h1')
        h2 = self.addHost('h2')
        switch = self.addSwitch('s0')

        # Add links with the given bandwidth, delay, and max queue
        self.addLink(h1, switch, bw=args.bw_host, delay=args.delay, max_queue_size=args.maxq)
        self.addLink(switch, h2, bw=args.bw_net, delay=args.delay, max_queue_size=args.maxq)

def start_qmon(iface, interval_sec=0.1, outfile="q.txt"):
    """
    Start a separate process to monitor queue length over time.
    """
    monitor = Process(target=monitor_qlen, args=(iface, interval_sec, outfile))
    monitor.start()
    return monitor

def start_ping(net):
    """
    Start a ping train from h1 to h2 to measure RTT during the experiment.
    """
    h1 = net.get('h1')
    h2 = net.get('h2')
    print("Starting ping train...")
    h1.popen(f"ping -i 0.1 -c {10 * args.time} {h2.IP()} > {args.dir}/ping.txt", shell=True)

def start_webserver(net):
    """
    Start a simple Python HTTP server on h1 to serve files (including message.txt).
    """
    h1 = net.get('h1')
    proc = h1.popen("python webserver.py", shell=True)
    sleep(1)
    return proc

def messages_bufferbloat():
    """
    Main function that sets up the experiment.
    Instead of a continuous large flow, we simulate short message transfers via TCP,
    but at a much higher rate to induce more bufferbloat.
    """
    if not os.path.exists(args.dir):
        os.makedirs(args.dir)

    # Set the congestion control algorithm
    os.system("sysctl -w net.ipv4.tcp_congestion_control=%s" % args.cong)

    # Clean up any leftover state
    cleanup()

    topo = BBTopo()
    net = Mininet(topo=topo, host=CPULimitedHost, link=TCLink)
    net.start()
    dumpNodeConnections(net.hosts)
    net.pingAll()

    # Start RTT measurement via ping
    start_ping(net)

    # Start queue monitoring
    qmon = start_qmon(iface='s0-eth2', outfile='%s/q.txt' % (args.dir))

    # Start the webserver on h1
    webserver_proc = start_webserver(net)

    h1 = net.get('h1')
    h2 = net.get('h2')

    # Simulate sending small messages for args.time seconds.
    # Now send messages at a much higher rate:
    # Random interval between messages is now between 0.001s and 0.01s.
    latencies = []
    start_time = time()
    while True:
        now = time()
        if now - start_time > args.time:
            break

        # Much smaller random interval to send more messages quickly
        interval = random.uniform(0.001, 0.01)
        sleep(interval)

        # Generate a random message
        random_msg = f"Random message: {random.randint(0, 100000)}"
        # Write the message to message.txt on h1
        h1.cmd(f"echo '{random_msg}' > message.txt")

        # Measure the time it takes to fetch the message from h2
        msg_start = time()
        h2.cmd(f"curl -o /dev/null -s -w '%{{time_total}}' http://{h1.IP()}:8000/message.txt > /dev/null 2>&1")
        msg_end = time()

        latencies.append(msg_end - msg_start)

    # Save latencies to a file
    with open('%s/latencies.txt' % args.dir, 'w') as f:
        for lat in latencies:
            f.write(f"{lat}\n")

    # Compute statistics
    if len(latencies) > 0:
        avg_time = sum(latencies) / len(latencies)
        stddev_time = math.sqrt(sum((x - avg_time)**2 for x in latencies) / len(latencies))
    else:
        avg_time = 0
        stddev_time = 0

    with open('%s/metrics.txt' % args.dir, 'w') as f:
        f.write(f"Average message latency: {avg_time:.4f} seconds\n")
        f.write(f"Standard deviation of latencies: {stddev_time:.4f} seconds\n")

    # Cleanup
    qmon.terminate()
    net.stop()

    # Kill the webserver if still running
    Popen("pgrep -f webserver.py | xargs kill -9", shell=True).wait()

if __name__ == "__main__":
    messages_bufferbloat()
