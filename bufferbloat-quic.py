from mininet.topo import Topo
from mininet.node import CPULimitedHost
from mininet.link import TCLink
from mininet.net import Mininet
from mininet.log import lg, info
from mininet.util import dumpNodeConnections
from mininet.cli import CLI
from mininet.clean import cleanup

from subprocess import Popen, PIPE
from time import sleep
from multiprocessing import Process
from argparse import ArgumentParser

from aioquic.asyncio import serve
from aioquic.quic.configuration import QuicConfiguration
from aioquic.h3.connection import H3Connection, H3_ALPN
from aioquic.asyncio.protocol import QuicConnectionProtocol

from monitor import monitor_qlen

import asyncio
import os

parser = ArgumentParser(description="Bufferbloat tests with QUIC")
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

args = parser.parse_args()

class BBTopo(Topo):
    "Simple topology for QUIC bufferbloat experiment."

    def build(self, n=2):
        h1 = self.addHost('h1')
        h2 = self.addHost('h2')
        switch = self.addSwitch('s0')

        self.addLink(h1, switch, bw=args.bw_host, delay=args.delay, max_queue_size=args.maxq)
        self.addLink(switch, h2, bw=args.bw_net, delay=args.delay, max_queue_size=args.maxq)

async def start_quic_server(host, port, certificate, private_key):
    """Start a QUIC server."""
    configuration = QuicConfiguration(is_client=False)
    configuration.alpn_protocols = H3_ALPN  # HTTP/3 ALPN
    configuration.load_cert_chain(certificate, private_key)

    print(f"Starting QUIC server on {host}:{port}...")
    server = await serve(
        host=host,
        port=port,
        configuration=configuration,
        create_protocol=Http3Server,
    )

    try:
        await asyncio.sleep(args.time)  # Keep the server running
    finally:
        server.close()
        await server.wait_closed()

class Http3Server(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._http = H3Connection(self._quic)

    def quic_event_received(self, event):
        # Handle QUIC events, such as stream data
        self._http.handle_event(event)

async def start_quic_client(host, port):
    """Start a QUIC client and request a resource."""
    configuration = QuicConfiguration(is_client=True)
    configuration.alpn_protocols = H3_ALPN

    print(f"Connecting to QUIC server at {host}:{port}...")
    async with serve(
        host=host,
        port=port,
        configuration=configuration,
    ) as protocol:
        http = H3Connection(protocol._quic)
        print("Client connected and ready to send requests.")
        # Example request
        try:
            response = await http.request(f"https://{host}:{port}/index.html")
            print("Response:", response)
        except Exception as e:
            print("Request failed:", e)

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

def bufferbloat():
    if not os.path.exists(args.dir):
        os.makedirs(args.dir)

    # Cleanup from last execution
    cleanup()

    topo = BBTopo()
    net = Mininet(topo=topo, host=CPULimitedHost, link=TCLink)
    net.start()
    dumpNodeConnections(net.hosts)
    net.pingAll()

    h1 = net.get('h1')
    h2 = net.get('h2')

    # Start Ping
    start_ping(net)

    # Start Queue Monitor
    qmon = start_qmon(iface='s0-eth2', outfile=f'{args.dir}/q.txt')

    # Start QUIC Server and Client
    loop = asyncio.get_event_loop()
    server_task = loop.create_task(
        start_quic_server(h1.IP(), 4433, "ssl_cert.pem", "ssl_key.pem")
    )
    client_task = loop.create_task(start_quic_client(h1.IP(), 4433))

    print("Experiment running...")
    loop.run_until_complete(asyncio.gather(server_task, client_task))

    # Cleanup
    qmon.terminate()
    net.stop()

if __name__ == "__main__":
    bufferbloat()
