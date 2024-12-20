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

import asyncio
import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from aioquic.quic.configuration import QuicConfiguration
from aioquic.asyncio import serve
from aioquic.asyncio.client import connect
from aioquic.h3.connection import H3Connection
from aioquic.h3.events import HeadersReceived, DataReceived, H3Event
from aioquic.asyncio.protocol import QuicConnectionProtocol

from monitor import monitor_qlen

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

        # h1 -- s0 -- h2
        self.addLink(h1, switch, bw=args.bw_host, delay="%dms" % args.delay, max_queue_size=args.maxq)
        self.addLink(switch, h2, bw=args.bw_net, delay="%dms" % args.delay, max_queue_size=args.maxq)

class Http3Server(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._http = H3Connection(self._quic)

    def quic_event_received(self, event):
        # Handle HTTP/3 events
        for http_event in self._http.handle_event(event):
            if isinstance(http_event, HeadersReceived):
                # Respond with a simple HTTP/3 response
                stream_id = http_event.stream_id
                self._http.send_headers(
                    stream_id=stream_id,
                    headers=[
                        (b":status", b"200"),
                        (b"content-type", b"text/plain"),
                    ],
                    end_stream=False,
                )
                body = b"Hello from QUIC server!"
                self._http.send_data(stream_id=stream_id, data=body, end_stream=True)
                self.transmit()

class Http3Client(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._http = H3Connection(self._quic)
        self._done = asyncio.Event()

    def quic_event_received(self, event):
        for http_event in self._http.handle_event(event):
            if isinstance(http_event, HeadersReceived):
                print("Client received headers:", http_event.headers)
            elif isinstance(http_event, DataReceived):
                print("Client received response body:", http_event.data.decode())
                if http_event.stream_ended:
                    self._done.set()

    def create_request(self, host, port):
        # Create a new stream and send a GET request
        stream_id = self._quic.get_next_available_stream_id()
        self._http.send_headers(
            stream_id=stream_id,
            headers=[
                (b":method", b"GET"),
                (b":scheme", b"https"),
                (b":authority", f"{host}:{port}".encode()),
                (b":path", b"/index.html"),
            ],
            end_stream=False,
        )
        self._http.send_data(stream_id=stream_id, data=b"", end_stream=True)
        self.transmit()

async def start_quic_server(host, port, certificate, private_key):
    configuration = QuicConfiguration(is_client=False)
    configuration.alpn_protocols = ["h3"]
    configuration.load_cert_chain(certificate, private_key)

    async with serve(
        host,
        port,
        configuration=configuration,
        create_protocol=Http3Server,
    ) as server:
        print(f"QUIC server running at {host}:{port}")
        await asyncio.sleep(args.time)

async def start_quic_client(host, port):
    configuration = QuicConfiguration(is_client=True)
    configuration.alpn_protocols = ["h3"]
    # In real scenarios, you'd load a CA or set verify_mode appropriately
    configuration.verify_mode = False

    async with connect(
        host,
        port,
        configuration=configuration,
        create_protocol=Http3Client,
    ) as client:
        client.create_request(host, port)
        await client._done.wait()

def start_qmon(iface, interval_sec=0.1, outfile="q.txt"):
    monitor = Process(target=monitor_qlen, args=(iface, interval_sec, outfile))
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
    # Identify which interface leads to h2:
    # Typically s0-eth2 or s0-eth1 depending on link order.
    # Check with "net.pingAll()" output or "dumpNodeConnections".
    # For simplicity, we assume s0-eth2 is correct.
    qmon = start_qmon(iface='s0-eth2', outfile=f'{args.dir}/q.txt')

    # Start QUIC Server on h2
    # We'll run the server in the event loop of h2 using its IP
    server_ip = h2.IP()

    # Start QUIC Client on h1
    # We'll connect to h2 from h1 using h2's IP
    client_ip = h1.IP()

    loop = asyncio.get_event_loop()
    server_task = loop.create_task(
        start_quic_server(server_ip, 4433, "ssl_cert.pem", "ssl_key.pem")
    )

    # Wait a moment for server to start
    # (Alternatively, you can run them concurrently if server is quick to start)
    # But let's just run both together:
    client_task = loop.create_task(start_quic_client(server_ip, 4433))

    print("Experiment running...")
    loop.run_until_complete(asyncio.gather(server_task, client_task))

    # Cleanup
    qmon.terminate()
    net.stop()

if __name__ == "__main__":
    bufferbloat()
