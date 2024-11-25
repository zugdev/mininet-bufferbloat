1. please setup TCP port forwarding 2223 in VM 22 in host for SSH login

to copy files from the VM use SCP

`scp -P 2223 -r mininet@localhost:~/mininet-bufferbloat ~/prog/mininet/receive-mini`

