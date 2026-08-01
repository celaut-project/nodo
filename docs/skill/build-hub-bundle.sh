#!/usr/bin/env bash
#
# build-hub-bundle.sh — assemble the distributable Celaut bridge-skill bundle.
#
# The in-repo skill (docs/skill/SKILL.md) links to its documentation by RELATIVE
# path (../INSTALL.md, ../PACKING.md, …) so a checkout stays a single source of
# truth. Hubs that host agent skills, however, want ONE flat, self-contained
# folder. This script generates that folder — docs/skill/skill-hub/ — by:
#
#   1. Deriving the file list from SKILL.md's own links (never hard-coded).
#   2. Failing loudly (non-zero, offending link named, nothing copied) on any
#      unresolvable target — the durable fix for stale/renamed refs (audit S-20).
#   3. Rewriting every in-repo relative link during the copy so the bundle is
#      flat: repo "../INSTALL.md" -> bundle "./INSTALL.md". Any in-repo file that
#      is NOT part of the bundle is rewritten to a pinned absolute upstream URL
#      (branch STABLE, never "master"), so no bundled link ever dangles.
#   4. Leaving absolute web URLs alone, but rejecting any that pin a nonexistent
#      "master" branch (repo has only stable/dev).
#   5. Stamping provenance (source commit SHA + build date) into MANIFEST.md.
#   6. Being idempotent + reproducible: skill-hub/ is cleared first, and the same
#      commit produces a byte-identical bundle (the SHA/date come from git, not
#      the wall clock, so a clean commit rebuilds identically).
#   7. Emitting a bundle that is git-ignored (see docs/skill/.gitignore).
#
# Usage:  docs/skill/build-hub-bundle.sh [--check]
#   --check  validate links / signatures and build to a temp dir, but do not
#            write skill-hub/ (used by CI).
#
set -euo pipefail

STABLE_BRANCH="stable"
UPSTREAM_BLOB="https://github.com/celaut-project/nodo/blob/${STABLE_BRANCH}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_MD="${SCRIPT_DIR}/SKILL.md"
REPO_ROOT="$(cd "${SCRIPT_DIR}" && git rev-parse --show-toplevel)"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

if [ ! -f "${SKILL_MD}" ]; then
  echo "FATAL: SKILL.md not found at ${SKILL_MD}" >&2
  exit 1
fi

fail() { echo "BUNDLE FAILED: $*" >&2; exit 1; }

# Provenance: prefer the committed SHA/date so a clean tree rebuilds identically.
if git -C "${REPO_ROOT}" diff --quiet HEAD -- "${REPO_ROOT}/docs" 2>/dev/null; then
  SRC_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD)"
  SRC_DATE="$(git -C "${REPO_ROOT}" show -s --format=%cs HEAD)"
else
  SRC_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD)-dirty"
  SRC_DATE="$(git -C "${REPO_ROOT}" show -s --format=%cs HEAD)"
  echo "WARN: docs/ has uncommitted changes; bundle stamped '${SRC_SHA}'." >&2
fi

# --- link extraction -------------------------------------------------------
# Print markdown link targets (the URL/path inside the parentheses), one per line.
extract_links() {
  grep -oE '\]\([^)]+\)' "$1" 2>/dev/null | sed -E 's/^\]\((.*)\)$/\1/' || true
}

# Resolve a relative link found in <src_file> to an absolute repo path (no anchor).
resolve_rel() {
  local src_file="$1" link="$2" base target
  link="${link%%#*}"                       # strip anchor
  [ -z "${link}" ] && { echo ""; return; } # pure anchor
  base="$(dirname "${src_file}")"
  target="$(cd "${base}" 2>/dev/null && cd "$(dirname "${link}")" 2>/dev/null && pwd)/$(basename "${link}")" || target=""
  echo "${target}"
}

is_abs_url() { [[ "$1" =~ ^https?:// ]]; }
is_anchor()  { [[ "$1" =~ ^# ]]; }

# --- 1. derive the bundle file set from SKILL.md's relative links ----------
# (No associative arrays: portable to bash 3.2. Membership is a newline-list test.)
BUNDLE_LIST=""             # newline-separated abs repo paths
BUNDLE_FILES=()            # ordered abs repo paths
in_bundle() { printf '%s\n' "${BUNDLE_LIST}" | grep -qxF "$1"; }
add_bundle() { local p="$1"; in_bundle "$p" && return; BUNDLE_LIST="${BUNDLE_LIST}
${p}"; BUNDLE_FILES+=("$p"); }

add_bundle "${SKILL_MD}"
while IFS= read -r link; do
  is_anchor "${link}" && continue
  is_abs_url "${link}" && continue
  abs="$(resolve_rel "${SKILL_MD}" "${link}")"
  [ -z "${abs}" ] && continue
  [ -f "${abs}" ] || fail "SKILL.md links '${link}' -> ${abs#${REPO_ROOT}/} which does not exist."
  add_bundle "${abs}"
done < <(extract_links "${SKILL_MD}")

# --- pin helper: repo-relative path -> stable upstream URL -----------------
upstream_url() { local abs="$1"; echo "${UPSTREAM_BLOB}/${abs#${REPO_ROOT}/}"; }

# --- 2/3/4. process each bundled file: validate + rewrite links ------------
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

for src in "${BUNDLE_FILES[@]}"; do
  out="${STAGE}/$(basename "${src}")"
  cp "${src}" "${out}"
  while IFS= read -r link; do
    is_anchor "${link}" && continue
    if is_abs_url "${link}"; then
      # 4. reject nonexistent 'master' branch pins.
      case "${link}" in
        */blob/master/*|*/raw/master/*|*/tree/master/*|*/master/*master*)
          fail "'$(basename "${src}")' pins a nonexistent 'master' branch: ${link}" ;;
        *//github.com/celaut-project/nodo/*/master/*)
          fail "'$(basename "${src}")' pins a nonexistent 'master' branch: ${link}" ;;
      esac
      continue
    fi
    anchor=""; [[ "${link}" == *#* ]] && anchor="#${link#*#}"
    bare="${link%%#*}"
    [ -z "${bare}" ] && continue
    abs="$(resolve_rel "${src}" "${bare}")"
    [ -n "${abs}" ] && [ -f "${abs}" ] || fail "'$(basename "${src}")' links '${bare}' -> unresolvable (${abs#${REPO_ROOT}/})."
    if in_bundle "${abs}"; then
      repl="./$(basename "${abs}")${anchor}"
    else
      repl="$(upstream_url "${abs}")${anchor}"     # exists in repo (checked above); pin to stable
    fi
    # Replace the exact "](link)" occurrence.
    esc_link="$(printf '%s' "${link}" | sed -e 's/[\/&]/\\&/g')"
    esc_repl="$(printf '%s' "${repl}" | sed -e 's/[\/&]/\\&/g')"
    sed -i.bak "s/](${esc_link})/](${esc_repl})/g" "${out}" && rm -f "${out}.bak"
  done < <(extract_links "${src}")
done

# --- verify: no bundled file retains a relative link that escapes the bundle
for out in "${STAGE}"/*.md; do
  while IFS= read -r link; do
    is_anchor "${link}" && continue
    is_abs_url "${link}" && continue
    bare="${link%%#*}"; [ -z "${bare}" ] && continue
    case "${bare}" in
      ./*) tgt="${STAGE}/${bare#./}"; [ -f "${tgt}" ] || fail "post-rewrite dangling link in $(basename "${out}"): ${link}" ;;
      *)   fail "post-rewrite non-flat relative link in $(basename "${out}"): ${link}" ;;
    esac
  done < <(extract_links "${out}")
done

# --- best-effort: SKILL.md nodo commands exist in the CLI dispatcher --------
NODO_PY="${REPO_ROOT}/nodo.py"
if [ -f "${NODO_PY}" ]; then
  known="$(grep -oE 'case "[a-z0-9_:]+"' "${NODO_PY}" | sed -E 's/case "(.*)"/\1/' | sort -u)"
  # also accept commands listed in the help block
  known="${known}
$(grep -oE '\\n- [a-z0-9_:]+' "${NODO_PY}" | sed -E 's/\\n- //' | sort -u)"
  # Only treat `nodo <cmd>` inside inline-code spans or at the start of a fenced
  # code line as a command invocation — never bare prose like "nodo can resolve".
  used="$( {
      grep -oE '`[^`]*`' "${SKILL_MD}" | grep -oE 'nodo [a-z0-9_:]+'
      grep -oE '^[[:space:]]*(sudo )?nodo [a-z0-9_:]+' "${SKILL_MD}"
    } | sed -E 's/.*nodo //' | sort -u)"
  missing=""
  while IFS= read -r cmd; do
    [ -z "${cmd}" ] && continue
    grep -qxF "${cmd}" <<<"${known}" || missing="${missing} ${cmd}"
  done <<<"${used}"
  if [ -n "${missing}" ]; then
    fail "SKILL.md references nodo command(s) not found in nodo.py dispatcher:${missing}"
  fi
fi

# --- 5. stamp MANIFEST -----------------------------------------------------
{
  echo "# Celaut bridge-skill hub bundle"
  echo
  echo "Generated by \`docs/skill/build-hub-bundle.sh\` — do not edit by hand."
  echo
  echo "- source: celaut-project/nodo @ \`${SRC_SHA}\`"
  echo "- source date: ${SRC_DATE}"
  echo "- links flattened to siblings; external URLs pinned to \`${STABLE_BRANCH}\`."
  echo
  echo "## Files"
  for src in "${BUNDLE_FILES[@]}"; do echo "- $(basename "${src}")  (from ${src#${REPO_ROOT}/})"; done
} > "${STAGE}/MANIFEST.md"

# --- publish ---------------------------------------------------------------
if [ "${CHECK_ONLY}" -eq 1 ]; then
  echo "OK (--check): $(( ${#BUNDLE_FILES[@]} )) files validated; links + signatures clean."
  exit 0
fi

OUT_DIR="${SCRIPT_DIR}/skill-hub"
rm -rf "${OUT_DIR}"
mkdir -p "${OUT_DIR}"
cp "${STAGE}"/*.md "${OUT_DIR}/"
echo "Wrote bundle -> ${OUT_DIR#${REPO_ROOT}/}/ ( $(( ${#BUNDLE_FILES[@]} )) docs + MANIFEST.md )"
echo "Source: ${SRC_SHA} (${SRC_DATE})"
