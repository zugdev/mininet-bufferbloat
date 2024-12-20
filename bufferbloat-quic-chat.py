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

parser = ArgumentParser(description="Chat-like small message tests with QUIC under messages_bufferbloat conditions")
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
    "Simple topology for chat-like small message experiment."
    def build(self, n=2):
        h1 = self.addHost('h1')
        h2 = self.addHost('h2')
        switch = self.addSwitch('s0')

        self.addLink(h1, switch, bw=args.bw_host, delay=args.delay, max_queue_size=args.maxq)
        self.addLink(switch, h2, bw=args.bw_net, delay=args.delay, max_queue_size=args.maxq)

def start_qmon(iface, interval_sec=0.1, outfile="q.txt"):
    """
    Start a queue monitor process that records queue lengths over time.
    """
    monitor = Process(target=monitor_qlen,
                      args=(iface, interval_sec, outfile))
    monitor.start()
    return monitor

def start_ping(net):
    """
    Start a ping train from h1 to h2 to record RTT values during the experiment.
    """
    h1 = net.get('h1')
    h2 = net.get('h2')
    print("Starting ping train...")
    h1.popen(f"ping -i 0.1 -c {10 * args.time} {h2.IP()} > {args.dir}/ping.txt", shell=True)

def start_quic_server(net):
    """
    Start a QUIC (HTTP/3) server on h2.
    The server should serve files from the directory it's run in.
    We assume 'http3_server.py' is in the same directory and will serve 'message.txt'.
    """
    h2 = net.get('h2')
    print("Starting QUIC server on h2...")
    server = h2.popen("python3 http3_server.py --certificate cert.pem --private-key key.pem --host 0.0.0.0 --port 4433", shell=True)
    sleep(2)
    return server

def messages_bufferbloat():
    """
    Main function to set up the topology, run the QUIC server,
    and simulate instant messaging by sending small messages at random intervals.
    """
    if not os.path.exists(args.dir):
        os.makedirs(args.dir)
    os.system("sysctl -w net.ipv4.tcp_congestion_control=%s" % args.cong)

    # Clean up any leftover mininet state from previous runs
    cleanup()

    topo = BBTopo()
    net = Mininet(topo=topo, host=CPULimitedHost, link=TCLink)
    net.start()
    dumpNodeConnections(net.hosts)
    net.pingAll()

    start_ping(net)

    # Start queue monitor on one of the switch interfaces
    qmon = start_qmon(iface='s0-eth2',
                      outfile='%s/q.txt' % (args.dir))

    # Start QUIC server on h2
    start_quic_server(net)

    h1 = net.get('h1')
    h2 = net.get('h2')

    # We will simulate sending short messages for args.time seconds.
    # For each message:
    #   1. Generate a random message string.
    #   2. Write it to 'message.txt' on h2.
    #   3. From h1, request (fetch) this file via QUIC.
    # Measure the latency of the fetch as the message delivery time.
    #
    # Messages are sent at random intervals between 0.1 and 1.0 seconds.

    latencies = []
    start_time = time()
    while True:
        now = time()
        delta = now - start_time
        if delta > args.time:
            break

        # Random interval between messages
        interval = random.uniform(0.1, 1.0)
        sleep(interval)

        # Generate a random message
        # This can be a random number or any random string.
        random_message = f"Random message: {random.randint(0, 100000)}"

        # Write this random message to message.txt on h2
        # We use echo to overwrite the file
        h2.cmd(f"echo '{random_message}' > message.txt")

        # Now measure how long it takes for h1 to fetch this message
        msg_start = time()
        h1.cmd(f"python3 http3_client.py --ca-certs cert.pem https://{h2.IP()}:4433/message.txt > /dev/null 2>&1")
        msg_end = time()

        # Record the latency
        latencies.append(msg_end - msg_start)

    # Save latencies to a file
    with open('%s/latencies.txt' % (args.dir), 'w') as f:
        for lat in latencies:
            f.write(f"{lat}\n")

    # Compute statistics
    if len(latencies) > 0:
        avg_time = sum(latencies) / len(latencies)
        stddev_time = math.sqrt(sum((x - avg_time) ** 2 for x in latencies) / len(latencies))
    else:
        avg_time = 0
        stddev_time = 0

    with open('%s/metrics.txt' % (args.dir), 'w') as f:
        f.write(f"Average message latency: {avg_time:.4f} seconds\n")
        f.write(f"Standard deviation of latencies: {stddev_time:.4f} seconds\n")

    qmon.terminate()
    net.stop()

    # Cleanup any remaining processes
    Popen("pgrep -f http3_server.py | xargs kill -9", shell=True).wait()
    Popen("pgrep -f http3_client.py | xargs kill -9", shell=True).wait()

if __name__ == "__main__":
    messages_bufferbloat()
