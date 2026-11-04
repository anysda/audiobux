#!/usr/bin/env bash
# Installs and starts audiobux on Debian 12 or 13 with an NVIDIA card:
# Docker and the NVIDIA Container Toolkit if they are missing, .env, the data
# folder, the containers. Safe to run again: what is in place is left alone.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$here"
export DEBIAN_FRONTEND=noninteractive
# seconds to wait for the worker and the web process after the start
WAIT=${AUDIOBUX_INSTALL_WAIT:-300}

say() { printf '==> %s\n' "$*"; }
fail() {
	printf 'Ошибка: %s\n' "$1" >&2
	shift
	for line in "$@"; do printf '  %s\n' "$line" >&2; done
	exit 1
}

# --- checks: nothing is installed before they pass

[ -r /etc/os-release ] || fail "нет /etc/os-release, систему не узнать." "Нужен Debian 12 или 13."
# shellcheck source=/dev/null
. /etc/os-release
if [ "${ID:-}" != debian ] || { [ "${VERSION_ID:-}" != 12 ] && [ "${VERSION_ID:-}" != 13 ]; }; then
	fail "нужен Debian 12 или 13, а здесь ${PRETTY_NAME:-неизвестная система}."
fi
say "Система: $PRETTY_NAME"

if ! command -v nvidia-smi >/dev/null; then
	fail "не найден nvidia-smi: драйвер NVIDIA не установлен." \
		"Установите драйвер карты (https://wiki.debian.org/NvidiaGraphicsDrivers)," \
		"проверьте, что nvidia-smi показывает карту, и запустите ./install.sh снова." \
		"Ничего не установлено."
fi
if ! gpus=$(nvidia-smi -L 2>&1) || ! grep -q '^GPU ' <<<"$gpus"; then
	fail "nvidia-smi не видит карту:" "$gpus" "Проверьте драйвер и запустите ./install.sh снова. Ничего не установлено."
fi
say "Карта: $(head -n1 <<<"$gpus")"

if [ "$(id -u)" -ne 0 ]; then
	command -v sudo >/dev/null || fail "нужны права root: запустите ./install.sh от root."
	say "Нужны права root, запускаю через sudo"
	exec sudo "$here/install.sh" "$@"
fi

# --- Docker and the NVIDIA Container Toolkit

apt_updated=
apt_install() {
	if [ -z "$apt_updated" ]; then
		apt-get update -q
		apt_updated=1
	fi
	apt-get install -y -q --no-install-recommends "$@"
}

# writes an apt source only when its text differs, then the lists are read again
put() {
	local text
	text=$(cat)
	if [ ! -f "$1" ] || [ "$(cat "$1")" != "$text" ]; then
		printf '%s\n' "$text" >"$1"
		apt_updated=
	fi
}

need_tools=()
command -v curl >/dev/null || need_tools+=(curl)
[ -f /etc/ssl/certs/ca-certificates.crt ] || need_tools+=(ca-certificates)
if [ "${#need_tools[@]}" -gt 0 ]; then
	say "Ставлю ${need_tools[*]}"
	apt_install "${need_tools[@]}"
fi

if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
	say "Docker уже стоит: $(docker --version)"
else
	say "Ставлю Docker из репозитория docker.com"
	install -m 0755 -d /etc/apt/keyrings
	[ -s /etc/apt/keyrings/docker.asc ] || curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
	chmod a+r /etc/apt/keyrings/docker.asc
	put /etc/apt/sources.list.d/docker.sources <<-EOF
		Types: deb
		URIs: https://download.docker.com/linux/debian
		Suites: $VERSION_CODENAME
		Components: stable
		Signed-By: /etc/apt/keyrings/docker.asc
	EOF
	if command -v docker >/dev/null; then
		apt_install docker-compose-plugin docker-buildx-plugin
	else
		apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
	fi
fi

if command -v nvidia-ctk >/dev/null; then
	say "NVIDIA Container Toolkit уже стоит: $(nvidia-ctk --version | head -n1)"
else
	say "Ставлю NVIDIA Container Toolkit"
	install -m 0755 -d /etc/apt/keyrings
	[ -s /etc/apt/keyrings/nvidia-container-toolkit.asc ] ||
		curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey -o /etc/apt/keyrings/nvidia-container-toolkit.asc
	chmod a+r /etc/apt/keyrings/nvidia-container-toolkit.asc
	put /etc/apt/sources.list.d/nvidia-container-toolkit.list \
		<<<"deb [signed-by=/etc/apt/keyrings/nvidia-container-toolkit.asc] https://nvidia.github.io/libnvidia-container/stable/deb/\$(ARCH) /"
	apt_install nvidia-container-toolkit
fi

systemd=
if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
	systemd=1
	systemctl enable --now docker >/dev/null
fi

daemon=/etc/docker/daemon.json
before=$(cat "$daemon" 2>/dev/null || true)
ctk=$(nvidia-ctk runtime configure --runtime=docker 2>&1) || fail "nvidia-ctk не настроил Docker:" "$ctk"
if [ "$(cat "$daemon" 2>/dev/null || true)" != "$before" ]; then
	if [ -n "$systemd" ]; then
		say "Рантайм nvidia добавлен в $daemon, перезапускаю Docker"
		systemctl restart docker
	else
		say "Рантайм nvidia добавлен в $daemon; systemd нет, перезапустите Docker сами"
	fi
fi
# inside LXC the cgroups of the devices belong to the host, and the toolkit may not
# touch them: the card goes in only with no-cgroups
where=$(systemd-detect-virt -c 2>/dev/null || tr '\0' '\n' </proc/1/environ 2>/dev/null | sed -n 's/^container=//p' || true)
case $where in lxc*)
	toolkit=/etc/nvidia-container-runtime/config.toml
	before=$(cat "$toolkit" 2>/dev/null || true)
	ctk=$(nvidia-ctk config --set nvidia-container-cli.no-cgroups=true --in-place 2>&1) ||
		fail "nvidia-ctk не включил no-cgroups для LXC:" "$ctk"
	[ "$(cat "$toolkit" 2>/dev/null || true)" = "$before" ] ||
		say "Контейнер LXC: в $toolkit включено no-cgroups"
	;;
esac
if ! docker info >/dev/null 2>&1; then
	if [ -n "$systemd" ]; then
		fail "Docker не отвечает." "Проверьте службу: systemctl status docker"
	fi
	fail "Docker не отвечает, а systemd здесь нет и запустить его некому." "Запустите dockerd сами и повторите ./install.sh."
fi

# --- settings and the data folder

if [ ! -f .env ]; then
	cp .env.example .env
	say "Создан .env из .env.example"
fi

# the last value of a variable in .env, without quotes
setting() { sed -n "s/^$1=//p" .env | tail -n1 | sed 's/^["'\'']//; s/["'\'']$//'; }
# sets a variable in .env, in place or at the end
set_setting() {
	if grep -q "^$1=" .env; then
		sed -i "s|^$1=.*|$1=$2|" .env
	else
		printf '%s=%s\n' "$1" "$2" >>.env
	fi
}

data=$(setting AUDIOBUX_DATA)
data=${data:-./data}
case $data in /*) ;; *) data=$here/${data#./} ;; esac
if [ ! -d "$data" ]; then
	mkdir -p "$data"
	# a new folder belongs to whoever owns the repository, not to root
	chown "$(stat -c %u:%g "$here")" "$data"
	say "Создан каталог данных $data"
fi
uid=$(stat -c %u "$data")
gid=$(stat -c %g "$data")
[ "$(setting AUDIOBUX_UID)" = "$uid" ] || set_setting AUDIOBUX_UID "$uid"
[ "$(setting AUDIOBUX_GID)" = "$gid" ] || set_setting AUDIOBUX_GID "$gid"
say "Данные: $data, владелец $uid:$gid"
port=$(setting AUDIOBUX_PORT)
port=${port:-8080}

# --- start and wait

say "Скачиваю образы и запускаю. Рантайм моделей (около 4 ГБ) и сами модели (около 28 ГБ) воркер скачает после запуска, ход виден в интерфейсе"
docker compose pull ||
	fail "образы не скачались." "Проверьте доступ к ghcr.io и запустите ./install.sh снова."
docker compose up -d ||
	fail "контейнеры не запустились." "Журналы: docker compose logs worker web"

hint="Журналы: docker compose logs worker web"
deadline=$((SECONDS + WAIT))
say "Жду воркер"
until [ "$(docker inspect -f '{{.State.Health.Status}}' audiobux-worker 2>/dev/null)" = healthy ]; do
	[ "$SECONDS" -lt "$deadline" ] || fail "воркер не поднялся за $WAIT с." "$hint"
	sleep 2
done
say "Жду веб"
until curl -fsS -o /dev/null "http://127.0.0.1:$port/api/status"; do
	[ "$SECONDS" -lt "$deadline" ] || fail "веб не ответил на порту $port за $WAIT с." "$hint"
	sleep 2
done

# the addresses of the machine in the LAN, without the bridges of Docker
if command -v ip >/dev/null; then
	addresses=$(ip -o -4 addr show scope global | awk '$2 !~ /^(docker|br-|veth)/ { split($4, a, "/"); print a[1] }')
else
	addresses=$(hostname -I)
fi
say "Готово. Интерфейс:"
for a in $addresses; do printf '    http://%s:%s\n' "$a" "$port"; done
say "Модели (около 28 ГБ) качаются в фоне, ход виден в интерфейсе; книги ждут их."
