# re-encore: docker compose wrapper with GPU auto-setup.
#
#   - NVIDIA GPU + driver on host, Container Toolkit missing -> installs it (asks sudo)
#   - no NVIDIA GPU at all                                        -> CPU fallback (SVT-AV1)
#
#   make up         build & start (auto GPU setup, see above)
#   make down       stop & remove container (state kept in ./data)
#   make restart    restart
#   make logs       follow app/encode logs
#   make status     container status + health
#   make gpu-test   verify NVENC works inside the container
#   make gpu-setup  install the NVIDIA Container Toolkit only

# Shell test: NVIDIA runtime registered in Docker AND a visible GPU.
# Recursive (=) so it is re-evaluated at recipe time, i.e. right after
# a toolkit install inside `make up`.
GPU_OK = docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -qi nvidia && nvidia-smi -L >/dev/null 2>&1

compose = docker compose $(shell if $(GPU_OK); then echo -f docker-compose.yml; else echo -f docker-compose.yml -f docker-compose.nogpu.yml; fi)

KEYRING = /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
GPU_INSTALL = curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o $(KEYRING) \
 && curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
     | sed 's|deb https://|deb [signed-by=$(KEYRING)] https://|g' \
     | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list \
 && sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit \
 && sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker

.PHONY: up down restart logs status gpu-test gpu-setup
up:
	@echo ">> mode: $(shell if $(GPU_OK); then echo 'GPU (NVENC)'; else echo 'CPU (SVT-AV1)'; fi)"
	@if ! (docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -qi nvidia) && nvidia-smi -L >/dev/null 2>&1; then \
	  if command -v apt-get >/dev/null 2>&1; then \
	    echo ">> NVIDIA GPU found but Container Toolkit missing, installing now (sudo required)"; \
	    $(GPU_INSTALL) && echo ">> Toolkit installed, GPU mode enabled"; \
	  else \
	    echo ">> WARNING: NVIDIA GPU found but this OS is not apt-based; install the Container Toolkit manually:"; \
	    echo "   https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"; \
	  fi; \
	fi
	$(compose) up -d --build

gpu-setup:
	$(GPU_INSTALL)

down:
	$(compose) down

restart:
	$(compose) restart

logs:
	$(compose) logs -f

status:
	$(compose) ps
	docker inspect --format='health: {{.State.Health.Status}}' re-encore

gpu-test:
	docker exec re-encore ffmpeg -hide_banner -f lavfi -i testsrc2 -t 1 -c:v av1_nvenc -f null -
