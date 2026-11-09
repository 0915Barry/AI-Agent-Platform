#!/usr/bin/env bash

# 从一行 /proc/<pid>/stat 文本解析字段 22 starttime。
# 第二个字段 comm 允许包含空格和括号，不能直接使用 awk '{print $22}'。
# Bash 的 ## 模式会删除到最后一个 ") " 为止，剩余数组从字段 3 state
# 开始，因此 starttime（字段 22）位于索引 19。
parse_process_start_ticks() {
  local stat_line="$1"
  local stat_tail=""
  local -a stat_fields=()

  [[ "${stat_line}" == *") "* ]] || return 1
  stat_tail="${stat_line##*) }"
  read -r -a stat_fields <<< "${stat_tail}"
  [[ "${#stat_fields[@]}" -gt 19 ]] || return 1
  [[ "${stat_fields[19]}" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "${stat_fields[19]}"
}


# 读取真实 Linux 进程的 stat，再交给统一文本解析函数。
process_start_ticks() {
  local pid="$1"
  local stat_path="/proc/${pid}/stat"
  local stat_line=""

  [[ -r "${stat_path}" ]] || return 1
  stat_line="$(<"${stat_path}")" || return 1
  parse_process_start_ticks "${stat_line}"
}
