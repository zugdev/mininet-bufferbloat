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

parser = ArgumentParser(description="Bufferbloat tests with QUIC (aioquic)")
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
    "Simple topology for bufferbloat experiment with QUIC."

    def build(self, n=2):
        h1 = self.addHost('h1')
        h2 = self.addHost('h2')
        switch = self.addSwitch('s0')

        self.addLink(h1, switch, bw=args.bw_host, delay=args.delay, max_queue_size=args.maxq)
        self.addLink(switch, h2, bw=args.bw_net, delay=args.delay, max_queue_size=args.maxq)

def start_qmon(iface, interval_sec=0.1, outfile="q.txt"):
    monitor = Process(target=monitor_qlen,
                      args=(iface, interval_sec, outfile))
    monitor.start()
    return monitor

def start_ping(net):
    h1 = net.get('h1')
    h2 = net.get('h2')
    print("Starting ping train...")
    h1.popen(f"ping -i 0.1 -c {10 * args.time} {h2.IP()} > {args.dir}/ping.txt", shell=True)

def start_quic_server(net):
    """
    Start a QUIC server (HTTP/3) on h2.
    We'll assume we have a file named 'index.html' in h2's directory,
    and we're running the aioquic example server on port 4433.

    Example command (adjust paths as needed):
    python3 /path/to/aioquic/examples/http3_server.py --certificate /path/to/cert.pem \
        --private-key /path/to/key.pem --host 0.0.0.0 --port 4433
    """
    h2 = net.get('h2')
    print("Starting QUIC server on h2...")
    server = h2.popen("python3 http3_server.py --certificate cert.pem --private-key key.pem --host 0.0.0.0 --port 4433", shell=True)
    sleep(2)
    return server

def start_quic_client(net):
    """
    Start a QUIC client on h1 that fetches a resource from h2.
    We will attempt a long-lived QUIC flow by repeatedly fetching a large file.
    If you want to simulate a long download, place a large file or repeatedly fetch the same file.
    """

    h1 = net.get('h1')
    h2 = net.get('h2')
    print("Starting QUIC client on h1...")
    # We'll run a loop from the main function, but here we show how we could do a single fetch:
    # h1.cmd("python3 http3_client.py https://%s:4433/index.html" % h2.IP())
    # For a long-lived flow, you might run a script on h1 that continuously requests data.
    # As a simple approximation, we start a background fetch process that runs until killed.
    # E.g., repeatedly curl via QUIC (http3_client.py). This is just an example. Adjust as needed.

    # Here we just return a Popen handle that fetches data continuously for `args.time` seconds.
    # A trick: run in a loop in the background. The server must have a file large enough or we repeatedly fetch.
    client = h1.popen(f"for i in $(seq 1 {args.time*10}); do python3 http3_client.py --ca-certs cert.pem https://{h2.IP()}:4433/index.html; sleep 1; done", shell=True)
    return client

def start_webserver(net):
    """
    This was used in the TCP version to host a webpage on h1.
    We could similarly have h1 host some files and h2 fetch them.
    But since we are testing QUIC (h2 as server), let's keep h1 as a client.
    If you still want h1 to run a webserver (over TCP) for comparison, you can keep this.
    Otherwise, this can be optional.
    """
    h1 = net.get('h1')
    proc = h1.popen("python3 webserver.py", shell=True)
    sleep(1)
    return [proc]

def measure_webpage_fetch_time(net):
    h2 = net.get('h2')
    times = []
    for _ in range(3):
        start = time()
        # For QUIC fetch times, we can still use curl if it supports HTTP/3 via QUIC.
        # If using aioquic's client directly:
        #   python3 http3_client.py --ca-certs cert.pem https://10.0.0.1:4433/index.html
        # We'll redirect output and capture the time.

        # The aioquic http3_client doesn't print time_total by default like curl does.
        # We need to measure time externally:
        # Just run the command and measure start/end times here.
        h2.cmd(f"python3 http3_client.py --ca-certs cert.pem https://10.0.0.1:4433/index.html > /dev/null 2>&1")
        end = time()
        times.append(end - start)
        sleep(5)
    return times

def bufferbloat():
    if not os.path.exists(args.dir):
        os.makedirs(args.dir)
    os.system("sysctl -w net.ipv4.tcp_congestion_control=%s" % args.cong)

    # cleanup last execution
    cleanup()

    topo = BBTopo()
    net = Mininet(topo=topo, host=CPULimitedHost, link=TCLink)
    net.start()
    dumpNodeConnections(net.hosts)
    net.pingAll()

    start_ping(net)

    qmon = start_qmon(iface='s0-eth2',
                      outfile='%s/q.txt' % (args.dir))

    # Start QUIC server and client
    start_quic_server(net)
    start_webserver(net)  # optional, if you still want a separate webserver on h1 for curl tests

    # Instead of iperf, we use a QUIC client to generate traffic
    quic_client_proc = start_quic_client(net)

    # Measure webpage fetch times using QUIC
    start_time = time()
    time_measures = []
    while True:
        now = time()
        delta = now - start_time
        if delta > args.time:
            break
        print("%.1fs left..." % (args.time - delta))

        h1 = net.get('h1')
        h2 = net.get('h2')

        # Measure the fetch time from h2 (client) fetching from h1 (server) if needed.
        # If h1 acts as server:
        # times = measure_webpage_fetch_time(net)
        # time_measures.extend(times)

        # Since in this example h2 is server and h1 is client, we might reverse the role here.
        # Let's do h1 fetching from h2 to measure times:
        start_fetch = time()
        h1.cmd(f"python3 http3_client.py --ca-certs cert.pem https://{h2.IP()}:4433/index.html > /dev/null 2>&1")
        end_fetch = time()
        time_measures.append(end_fetch - start_fetch)

        sleep(5)

    # Compute statistics
    with open('%s/metrics.txt' % (args.dir), 'w') as f:
        avg_time = sum(time_measures) / len(time_measures) if time_measures else 0
        stddev_time = math.sqrt(sum((x - avg_time) ** 2 for x in time_measures) / len(time_measures)) if time_measures else 0
        f.write(f"Average fetch time: {avg_time:.4f} seconds\n")
        f.write(f"Standard deviation of fetch times: {stddev_time:.4f} seconds\n")

    quic_client_proc.terminate()
    qmon.terminate()
    net.stop()
    Popen("pgrep -f webserver.py | xargs kill -9", shell=True).wait()
    # If there are any aioquic server/client processes lingering, kill them as well:
    Popen("pgrep -f http3_server.py | xargs kill -9", shell=True).wait()
    Popen("pgrep -f http3_client.py | xargs kill -9", shell=True).wait()

if __name__ == "__main__":
    bufferbloat()
