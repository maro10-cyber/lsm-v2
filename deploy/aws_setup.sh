#!/bin/bash
# Run this once on a fresh Ubuntu 22.04 AWS EC2 instance.
# Recommended: t3.micro (free tier) or t3.small

set -e

echo "=== Installing Docker ==="
sudo apt-get update -y
sudo apt-get install -y docker.io docker-compose-v2 git
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker ubuntu

echo "=== Cloning repo ==="
# Replace with your GitHub repo URL after pushing
git clone https://github.com/YOUR_USERNAME/lsm-v2.git /home/ubuntu/lsm-v2
cd /home/ubuntu/lsm-v2

echo "=== Creating .env file ==="
cat > .env <<EOF
TV_USERNAME=your_tradovate_username
TV_PASSWORD=your_tradovate_password
TV_CID=your_cid
TV_SECRET=your_secret
TV_DEMO=true
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
echo "Check logs:  sudo journalctl -u lsm-paper -f"
echo "Or:          docker compose logs -f paper"
echo "Edit creds:  nano /home/ubuntu/lsm-v2/.env && sudo systemctl restart lsm-paper"
