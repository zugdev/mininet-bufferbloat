import os
import matplotlib.pyplot as plt

# List of directories to process
directories = [
    "quic-chat-q20",
    "quic-chat-q100",
    "bbr-chat-q20",
    "bbr-chat-q100",
    "reno-chat-q20",
    "reno-chat-q100"
]

for data_dir in directories:
    latencies_file = os.path.join(data_dir, "latencies.txt")
    metrics_file = os.path.join(data_dir, "metrics.txt")

    # Check if required files exist
    if not os.path.exists(latencies_file) or not os.path.exists(metrics_file):
        print(f"Skipping {data_dir}: Missing latencies.txt or metrics.txt")
        continue

    # Read latencies
    latencies = []
    with open(latencies_file, 'r') as f:
        for line in f:
            val = line.strip()
            if val:
                latencies.append(float(val))

    # Read average and stddev from metrics.txt
    avg_latency = None
    stddev_latency = None
    with open(metrics_file, 'r') as f:
        for line in f:
            line_lower = line.strip().lower()
            if "average message latency:" in line_lower:
                # Extract the numeric value
                avg_latency = float(line_lower.split(":")[-1].strip().split()[0])
            elif "standard deviation of latencies:" in line_lower:
                stddev_latency = float(line_lower.split(":")[-1].strip().split()[0])

    # Plot latencies
    plt.figure(figsize=(10,6))
    plt.plot(latencies, label='Latencies', color='blue')

    # Plot average line
    if avg_latency is not None:
        plt.axhline(y=avg_latency, color='red', linestyle='--', label=f'Average = {avg_latency:.4f} s')

    # Plot ±1 stddev lines if stddev is available
    if avg_latency is not None and stddev_latency is not None:
        plt.axhline(y=avg_latency + stddev_latency, color='green', linestyle=':', 
                    label=f'+1 Std Dev = {(avg_latency + stddev_latency):.4f} s')
        plt.axhline(y=avg_latency - stddev_latency, color='green', linestyle=':',
                    label=f'-1 Std Dev = {(avg_latency - stddev_latency):.4f} s')

    plt.title(f"Message Latencies Over Time - {data_dir}")
    plt.xlabel("Message Index")
    plt.ylabel("Latency (seconds)")
    plt.legend()
    plt.grid(True)

    # Save the figure with the directory name in the filename
    out_file = f"{data_dir}-latency.png"
    plt.savefig(out_file)
    plt.close()

    print(f"Plot saved as {out_file}")
