#!/usr/bin/env python3
"""The one use of xxd that libvmaf's build makes, `xxd -i [-n NAME] IN OUT`, for SDKs that
don't ship xxd. libvmaf embeds its VMAF models with it; without it the models are left out
and `libvmaf=model=version=vmaf_v0.6.1` fails at run time. Installed only while building."""
import re
import sys


def main(args: list[str]) -> int:
    name = None
    if "-n" in args:
        i = args.index("-n")
        name = args[i + 1]
        del args[i:i + 2]
    if "-i" not in args:
        return 0            # libvmaf's "does xxd take -n" probe: yes
    args.remove("-i")
    src, dst = args
    name = re.sub(r"[^0-9A-Za-z_]", "_", name or src)
    data = open(src, "rb").read()
    rows = [", ".join(f"0x{b:02x}" for b in data[i:i + 12]) for i in range(0, len(data), 12)]
    with open(dst, "w") as out:
        out.write(f"unsigned char {name}[] = {{\n  " + ",\n  ".join(rows) + "\n};\n")
        out.write(f"unsigned int {name}_len = {len(data)};\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
