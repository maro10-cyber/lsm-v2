#!/bin/bash
# Run once on a fresh Ubuntu 22.04 AWS EC2 instance.
# Recommended: t3.small (IB Gateway needs ~1GB RAM)

set -e

echo "=== Installing Docker ==="
sudo apt-get update -y
sudo apt-get install -y docker.io docker-compose-v2 git
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker ubuntu

echo "=== Cloning repo ==="
git clone https://github.com/maro10-cyber/lsm-v2.git /home/ubuntu/lsm-v2
cd /home/ubuntu/lsm-v2

echo "=== Creating .env file ==="
cat > .env <<EOF
IB_USERNAME=nbhojz092
IB_PASSWORD=YOUR_PAPER_PASSWORD_HERE
IB_ACCOUNT=DUO386766
EOF
chmod 600 .env

echo "=== Building Docker image ==="
docker compose build

echo "=== Installing systemd service ==="
sudo cp deploy/lsm-paper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable lsm-paper
sudo systemctl start lsm-paper

echo ""
echo "=== Done! ==="
echo "Edit password first:  nano /home/ubuntu/lsm-v2/.env"
echo "Then restart:         sudo systemctl restart lsm-paper"
echo "Watch logs:           docker compose logs -f"
