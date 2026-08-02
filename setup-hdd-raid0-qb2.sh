#!/usr/bin/env bash
set -Eeuo pipefail

# Detect unused rotational disks, create an ext4 RAID0, and optionally point
# qBittorrent at the resulting download directory.

MD_DEVICE="${MD_DEVICE:-/dev/md0}"
MOUNT_POINT="${MOUNT_POINT:-/mnt/raid0}"
DOWNLOAD_DIR="${DOWNLOAD_DIR:-${MOUNT_POINT}/downloads}"
QB_SAVE_PATH="${QB_SAVE_PATH:-${DOWNLOAD_DIR}}"
QB_URL=""
QB_USERNAME=""
QB_PASSWORD=""
FS_LABEL="${FS_LABEL:-raid0}"
CHUNK_KIB="${CHUNK_KIB:-512}"
DOWNLOAD_MODE="${DOWNLOAD_MODE:-0777}"
DOWNLOAD_OWNER="${DOWNLOAD_OWNER:-}"
DRY_RUN=0
ASSUME_YES=0
INTERACTIVE_QB=0

usage() {
  cat <<'EOF'
用法：sudo bash setup-hdd-raid0-qb2.sh [-q] [--dry-run] [--yes]

脚本自动选择旋转式、非移动、可写、非系统盘且没有已挂载分区的整块 HDD。
创建 RAID0 会清空所有候选硬盘上的数据。

选项：
  -q         交互输入 qBittorrent 端口、用户名和密码，并设置下载目录
  --dry-run  只显示候选硬盘和操作，不修改任何内容
  --yes      跳过新建 RAID0 前的擦盘确认
  -h, --help 显示帮助

环境变量：
  MD_DEVICE       RAID 设备                         （默认：/dev/md0）
  MOUNT_POINT     宿主机挂载点                      （默认：/mnt/raid0）
  DOWNLOAD_DIR    宿主机下载目录                    （默认：<挂载点>/downloads）
  QB_SAVE_PATH    qBittorrent 内可见的目录          （默认：DOWNLOAD_DIR）
  FS_LABEL        ext4 卷标                         （默认：raid0）
  CHUNK_KIB       RAID0 块大小，单位 KiB            （默认：512）
  DOWNLOAD_MODE   下载目录权限                      （默认：0777）
  DOWNLOAD_OWNER  可选目录所有者，如 1000:1000     （默认：不修改）

示例：
  sudo bash setup-hdd-raid0-qb2.sh -q
  sudo bash setup-hdd-raid0-qb2.sh -q --dry-run
  sudo bash setup-hdd-raid0-qb2.sh --dry-run
  sudo bash setup-hdd-raid0-qb2.sh
EOF
}

log() { printf '[raid0-qb2] %s\n' "$*"; }
die() { printf '[raid0-qb2] 错误：%s\n' "$*" >&2; exit 1; }

run() {
  if ((DRY_RUN)); then
    printf '[预演]'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

while (($#)); do
  case "$1" in
    -q) INTERACTIVE_QB=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --yes) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "未知选项：$1" ;;
  esac
  shift
done

((EUID == 0)) || die "请使用 root 用户运行（sudo）。"

if ((INTERACTIVE_QB)); then
  printf '请输入 qBittorrent WebUI 端口：'
  read -r qb_port_input
  [[ "$qb_port_input" =~ ^[0-9]{1,5}$ ]] || \
    die "qBittorrent 端口必须是 1 到 65535 之间的整数。"
  qb_port_number=$((10#$qb_port_input))
  ((qb_port_number >= 1 && qb_port_number <= 65535)) || \
    die "qBittorrent 端口必须是 1 到 65535 之间的整数。"

  printf '请输入 qBittorrent 用户名：'
  read -r QB_USERNAME
  printf '请输入 qBittorrent 密码：'
  read -rs QB_PASSWORD
  printf '\n'
  QB_URL="http://127.0.0.1:${qb_port_number}"
fi

for command_name in lsblk findmnt mountpoint awk grep sed mdadm mkfs.ext4 tune2fs blkid mount umount wipefs; do
  command -v "$command_name" >/dev/null 2>&1 || die "缺少命令：$command_name"
done
if ((INTERACTIVE_QB)); then
  for command_name in curl readlink; do
    command -v "$command_name" >/dev/null 2>&1 || die "缺少命令：$command_name"
  done
fi

[[ "$MD_DEVICE" == /dev/* ]] || die "MD_DEVICE 必须是 /dev 下的绝对路径。"
[[ "$MOUNT_POINT" == /* && "$DOWNLOAD_DIR" == /* && "$QB_SAVE_PATH" == /* ]] || \
  die "MOUNT_POINT、DOWNLOAD_DIR 和 QB_SAVE_PATH 必须是绝对路径。"
[[ "$DOWNLOAD_DIR" == "$MOUNT_POINT"/* ]] || die "DOWNLOAD_DIR 必须位于 MOUNT_POINT 内。"
[[ "$MOUNT_POINT$DOWNLOAD_DIR$QB_SAVE_PATH" != *[$'\n\r\t ']* ]] || \
  die "路径不能包含空格或控制字符。"
[[ "$MOUNT_POINT$DOWNLOAD_DIR$QB_SAVE_PATH" =~ ^[A-Za-z0-9_./-]+$ ]] || \
  die "路径只能包含字母、数字、下划线、点、斜杠和连字符。"
[[ "$CHUNK_KIB" =~ ^[0-9]+$ ]] || die "CHUNK_KIB 必须是整数。"
[[ "$DOWNLOAD_MODE" =~ ^0?[0-7]{3}$ ]] || die "DOWNLOAD_MODE 必须是 0777 这样的八进制权限。"

declare -A system_disks=()
for mount_target in / /boot /boot/efi; do
  while IFS= read -r source_device; do
    [[ "$source_device" == /dev/* ]] || continue
    while read -r block_name block_type; do
      [[ "$block_type" == disk ]] && system_disks["/dev/$block_name"]=1
    done < <(lsblk -s -nro NAME,TYPE "$source_device" 2>/dev/null || true)
  done < <(findmnt -rn -o SOURCE --target "$mount_target" 2>/dev/null || true)
done

# ZFS mount sources are dataset names rather than /dev paths, so include every
# physical disk backing an imported pool in the system-disk exclusion set.
if command -v zpool >/dev/null 2>&1; then
  while IFS= read -r zfs_device; do
    while read -r block_name block_type; do
      [[ "$block_type" == disk ]] && system_disks["/dev/$block_name"]=1
    done < <(lsblk -s -nro NAME,TYPE "$zfs_device" 2>/dev/null || true)
  done < <(zpool status -P 2>/dev/null | awk '$1 ~ /^\/dev\// {print $1}')
fi

mapfile -t all_hdds < <(
  lsblk -dnro NAME,TYPE,ROTA,RM,RO |
    awk '$2 == "disk" && $3 == 1 && $4 == 0 && $5 == 0 {print "/dev/" $1}'
)

candidate_disks=()
for disk in "${all_hdds[@]}"; do
  [[ -n "${system_disks[$disk]:-}" ]] && continue
  if lsblk -nrpo MOUNTPOINT "$disk" | grep -q '[^[:space:]]'; then
    continue
  fi
  if lsblk -nrpo TYPE "$disk" | awk 'NR > 1' | grep -Eq '^(raid|lvm|crypt|mpath)'; then
    continue
  fi
  candidate_disks+=("$disk")
done

raid_exists=0
format_existing=0
if [[ -b "$MD_DEVICE" ]]; then
  raid_exists=1
  raid_level="$(mdadm --detail "$MD_DEVICE" | awk -F': ' '/Raid Level/ {gsub(/[[:space:]]/, "", $2); print $2; exit}')"
  [[ "$raid_level" == raid0 ]] || die "检测到 $MD_DEVICE，但它不是 RAID0。"
  existing_fs_type="$(blkid -s TYPE -o value "$MD_DEVICE" 2>/dev/null || true)"
  log "检测到已有 RAID0：$MD_DEVICE（文件系统：${existing_fs_type:-无}）"
  while true; do
    printf '是否重新格式化 %s？这会清空阵列中的全部数据，请输入 yes 或 no：' "$MD_DEVICE"
    read -r format_answer || die "未读取到回答，操作已取消。"
    case "$format_answer" in
      yes) format_existing=1; break ;;
      no) format_existing=0; break ;;
      *) log "输入无效，请完整输入 yes 或 no。" ;;
    esac
  done
  if ((format_existing == 0)); then
    [[ "$existing_fs_type" == ext4 ]] || \
      die "选择了不格式化，但 $MD_DEVICE 不是可复用的 ext4 文件系统。"
    log "将保留现有 ext4 文件系统。"
  fi
else
  ((${#candidate_disks[@]} >= 2)) || die \
    "只找到 ${#candidate_disks[@]} 块符合条件的 HDD；RAID0 至少需要 2 块。"

  log "符合条件的 HDD（这些硬盘上的全部数据将被清除）："
  lsblk -d -o NAME,SIZE,MODEL,SERIAL,ROTA,TRAN "${candidate_disks[@]}"
fi

if ((DRY_RUN)); then
  if ((raid_exists == 0)); then
    log "将使用 ${#candidate_disks[@]} 块硬盘创建 RAID0：$MD_DEVICE"
    run mdadm --create "$MD_DEVICE" --verbose --force --level=0 \
      --raid-devices="${#candidate_disks[@]}" --chunk="$CHUNK_KIB" "${candidate_disks[@]}"
    run mkfs.ext4 -F -m 0 -L "$FS_LABEL" "$MD_DEVICE"
  elif ((format_existing)); then
    run mkfs.ext4 -F -m 0 -L "$FS_LABEL" "$MD_DEVICE"
  fi
  if ((INTERACTIVE_QB)); then
    run mkdir -p "$DOWNLOAD_DIR"
  fi
  log "将把 $MD_DEVICE 挂载到 $MOUNT_POINT。"
  if ((INTERACTIVE_QB)); then
    log "将把 qBittorrent 下载目录设置为 $QB_SAVE_PATH。"
  fi
  exit 0
fi

# Verify WebUI access before performing any destructive disk operation.
if ((INTERACTIVE_QB)); then
  cookie_file="$(mktemp)"
  trap 'rm -f "$cookie_file"' EXIT
  curl_common=(--silent --show-error --fail --cookie "$cookie_file" --cookie-jar "$cookie_file" --referer "$QB_URL/")
  login_response="$(curl "${curl_common[@]}" \
    --data-urlencode "username=$QB_USERNAME" \
    --data-urlencode "password=$QB_PASSWORD" \
    "$QB_URL/api/v2/auth/login")" || die "无法登录 qBittorrent：$QB_URL"
  [[ "$login_response" == Ok. ]] || die "qBittorrent 登录失败：$login_response"
  curl "${curl_common[@]}" "$QB_URL/api/v2/app/preferences" >/dev/null || \
    die "无法读取 qBittorrent 设置，请检查端口、用户名和密码。"
fi

if ((raid_exists == 0)); then
  if ((ASSUME_YES == 0)); then
    printf '\n请输入 ERASE-AND-CREATE-RAID0 以确认清空候选硬盘：'
    read -r confirmation
    [[ "$confirmation" == ERASE-AND-CREATE-RAID0 ]] || die "操作已取消，硬盘未被修改。"
  fi

  for disk in "${candidate_disks[@]}"; do
    run wipefs --all --force "$disk"
    mdadm --zero-superblock --force "$disk" >/dev/null 2>&1 || true
  done

  run mdadm --create "$MD_DEVICE" --verbose --force --level=0 \
    --raid-devices="${#candidate_disks[@]}" --chunk="$CHUNK_KIB" "${candidate_disks[@]}"
  command -v udevadm >/dev/null 2>&1 && udevadm settle
fi

if ((raid_exists && format_existing)); then
  mapfile -t existing_mounts < <(lsblk -nrpo MOUNTPOINT "$MD_DEVICE" | awk 'NF')
  for existing_mount in "${existing_mounts[@]}"; do
    run umount "$existing_mount"
  done
  run mkfs.ext4 -F -m 0 -L "$FS_LABEL" "$MD_DEVICE"
elif ! blkid "$MD_DEVICE" >/dev/null 2>&1; then
  run mkfs.ext4 -F -m 0 -L "$FS_LABEL" "$MD_DEVICE"
else
  fs_type="$(blkid -s TYPE -o value "$MD_DEVICE")"
  [[ "$fs_type" == ext4 ]] || die "$MD_DEVICE 的文件系统是 $fs_type，需要 ext4。"
  run tune2fs -m 0 "$MD_DEVICE"
fi

run mkdir -p "$MOUNT_POINT"
fs_uuid="$(blkid -s UUID -o value "$MD_DEVICE")"
[[ -n "$fs_uuid" ]] || die "无法读取 $MD_DEVICE 的文件系统 UUID。"

fstab_source="$(awk -v target="$MOUNT_POINT" '$1 !~ /^#/ && $2 == target {print $1; exit}' /etc/fstab)"
if [[ -z "$fstab_source" ]]; then
  printf 'UUID=%s %s ext4 defaults,noatime,nofail 0 2\n' "$fs_uuid" "$MOUNT_POINT" >>/etc/fstab
  log "已将 $MOUNT_POINT 写入 /etc/fstab。"
elif [[ "$fstab_source" != "UUID=$fs_uuid" && "$fstab_source" != "$MD_DEVICE" ]]; then
  fstab_tmp="$(mktemp)"
  awk -v target="$MOUNT_POINT" '$1 ~ /^#/ || $2 != target {print}' /etc/fstab >"$fstab_tmp"
  printf 'UUID=%s %s ext4 defaults,noatime,nofail 0 2\n' "$fs_uuid" "$MOUNT_POINT" >>"$fstab_tmp"
  cat "$fstab_tmp" >/etc/fstab
  rm -f "$fstab_tmp"
  log "已更新 /etc/fstab 中 $MOUNT_POINT 的 UUID。"
fi

if ! mountpoint -q "$MOUNT_POINT"; then
  run mount "$MOUNT_POINT"
fi
expected_device_number="$(lsblk -dnro MAJ:MIN "$MD_DEVICE")"
mounted_device_number="$(findmnt -nro MAJ:MIN --target "$MOUNT_POINT")"
[[ "$mounted_device_number" == "$expected_device_number" ]] || \
  die "$MOUNT_POINT 挂载的不是 $MD_DEVICE。"
if ((INTERACTIVE_QB)); then
  run mkdir -p "$DOWNLOAD_DIR"
  run chmod "$DOWNLOAD_MODE" "$DOWNLOAD_DIR"
  if [[ -n "$DOWNLOAD_OWNER" ]]; then
    run chown "$DOWNLOAD_OWNER" "$DOWNLOAD_DIR"
  fi
fi

# Record the array for assembly after reboot when the distribution has an mdadm config.
if [[ -f /etc/mdadm/mdadm.conf ]]; then
  md_uuid="$(mdadm --detail "$MD_DEVICE" | awk -F': ' '/UUID/ {gsub(/[[:space:]]/, "", $2); print $2; exit}')"
  mdadm_line="$(mdadm --detail --scan | awk -v uuid="$md_uuid" 'index($0, "UUID=" uuid) {print; exit}')"
  if [[ -n "$mdadm_line" ]]; then
    if [[ -n "$md_uuid" ]] && ! grep -qF "UUID=$md_uuid" /etc/mdadm/mdadm.conf; then
      printf '%s\n' "$mdadm_line" >>/etc/mdadm/mdadm.conf
      command -v update-initramfs >/dev/null 2>&1 && update-initramfs -u
    fi
  fi
elif [[ -f /etc/mdadm.conf ]]; then
  md_uuid="$(mdadm --detail "$MD_DEVICE" | awk -F': ' '/UUID/ {gsub(/[[:space:]]/, "", $2); print $2; exit}')"
  mdadm_line="$(mdadm --detail --scan | awk -v uuid="$md_uuid" 'index($0, "UUID=" uuid) {print; exit}')"
  if [[ -n "$mdadm_line" ]]; then
    if [[ -n "$md_uuid" ]] && ! grep -qF "UUID=$md_uuid" /etc/mdadm.conf; then
      printf '%s\n' "$mdadm_line" >>/etc/mdadm.conf
    fi
  fi
fi

if ((INTERACTIVE_QB)); then
  # If qb2 is a Docker container, the API path must exist inside that container.
  if command -v docker >/dev/null 2>&1 && docker inspect qb2 >/dev/null 2>&1; then
    docker_path_is_mapped=0
    host_download_real="$(readlink -f "$DOWNLOAD_DIR")"
    while IFS='|' read -r host_source container_destination; do
      container_destination="${container_destination%/}"
      if [[ "$QB_SAVE_PATH" == "$container_destination" || \
            "$QB_SAVE_PATH" == "$container_destination"/* ]]; then
        path_suffix="${QB_SAVE_PATH#"$container_destination"}"
        mapped_host_path="$(readlink -f "${host_source%/}${path_suffix}")"
        if [[ "$mapped_host_path" == "$host_download_real" ]]; then
          docker_path_is_mapped=1
          break
        fi
      fi
    done < <(docker inspect --format '{{range .Mounts}}{{printf "%s|%s\n" .Source .Destination}}{{end}}' qb2)

    ((docker_path_is_mapped == 1)) || \
      die "qb2 容器中的 $QB_SAVE_PATH 没有映射到宿主机目录 $DOWNLOAD_DIR。RAID0 已设置完成。"
    if ! docker exec qb2 test -d "$QB_SAVE_PATH"; then
      die "qb2 容器无法访问 $QB_SAVE_PATH。请绑定 $DOWNLOAD_DIR 并用 QB_SAVE_PATH 指定容器内路径。RAID0 已设置完成。"
    fi
  fi

  escaped_qb_path="${QB_SAVE_PATH//\\/\\\\}"
  escaped_qb_path="${escaped_qb_path//\"/\\\"}"
  preferences_json="{\"save_path\":\"$escaped_qb_path\"}"
  curl "${curl_common[@]}" --data-urlencode "json=$preferences_json" \
    "$QB_URL/api/v2/app/setPreferences" >/dev/null || \
    die "无法修改 qBittorrent 下载目录。"

  current_preferences="$(curl "${curl_common[@]}" "$QB_URL/api/v2/app/preferences")" || \
    die "已提交 qBittorrent 设置，但无法回读验证。"
  compact_preferences="$(tr -d '[:space:]' <<<"$current_preferences")"
  returned_save_path="$(sed -n 's/.*"save_path":"\([^"]*\)".*/\1/p' <<<"$compact_preferences")"
  [[ "${returned_save_path%/}" == "${QB_SAVE_PATH%/}" ]] || \
    die "qBittorrent 返回的下载目录是 $returned_save_path，不是 $QB_SAVE_PATH。"
fi

log "RAID0 设置完成：$MD_DEVICE 已挂载到 $MOUNT_POINT。"
if ((INTERACTIVE_QB)); then
  log "qBittorrent（$QB_URL）的下载目录已设置为 $QB_SAVE_PATH。"
fi
