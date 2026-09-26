# NVIDIA ships Nsight Graphics for Linux as a Makeself .run bundling its own
# Qt and most deps. It is unpacked as-is into the store and run inside an FHS
# env that supplies the remaining system libraries. The ELF binaries are left
# unpatched: ngfx injects libraries into the target app and loads its own
# Vulkan layers by path, which patchelf-ing tends to break.
{
  lib,
  stdenvNoCC,
  fetchurl,
  buildFHSEnv,
  writeShellScript,
  runCommand,
  libx11,
  libxfixes,
  libxi,
  libxcb-keysyms,
  python3,
  analyzerSrc ? null,
}:
let
  # Pinned to 2026.1 (needs driver 580.126.18+); 2026.3 needs 615+, which
  # nixpkgs does not ship yet.
  version = "2026.1.0.26067";
  folder = "NVIDIA-Nsight-Graphics-2026.1";

  unwrapped = stdenvNoCC.mkDerivation {
    pname = "nsight-graphics-unwrapped";
    inherit version;

    src = fetchurl {
      url = "https://developer.nvidia.com/downloads/assets/tools/secure/nsight-graphics/2026_1_0/linux_x64/NVIDIA_Nsight_Graphics_${version}-linux_x64.run";
      hash = "sha256-6YhQKVrXxVWrsr29iTv4fbRIvz1RgPOhxLLq3uZ13vo=";
    };

    dontUnpack = true;
    dontFixup = true;

    # Same folder name the upstream installer creates; the analyzer parses the
    # version out of it.
    installPhase = ''
      sh $src --noexec --noprogress --nox11 --target extract
      mkdir -p $out
      cp -r extract/pkg $out/${folder}
    '';
  };

  hostDir = "${unwrapped}/${folder}/host/linux-desktop-nomad-x64";

  tools = [
    "ngfx"
    "ngfx-ui"
    "ngfx-capture"
    "ngfx-replay"
    "nv-aftermath-monitor"
  ];

  # Nsight's hooks in the target dlopen these by soname and call through the
  # result unchecked. Nix-built apps find them via RUNPATH, which does not
  # apply to those dlopen calls, so without this the target segfaults on
  # its first XOpenDisplay.
  hookLibs = runCommand "nsight-graphics-hook-libs" { } ''
    mkdir -p $out/lib
    ln -s ${libx11}/lib/libX11.so.6 ${libx11}/lib/libX11-xcb.so.1 \
      ${libxfixes}/lib/libXfixes.so.3 ${libxi}/lib/libXi.so.6 \
      ${libxcb-keysyms}/lib/libxcb-keysyms.so.1 $out/lib/
  '';

  # `nsight-graphics <tool> [args]` runs a host tool; `nsight-analyzer` is
  # dispatched the same way so it sees the same FHS env as ngfx.
  dispatch = writeShellScript "nsight-graphics-dispatch" ''
    export NSIGHT_GRAPHICS_DIR=${hostDir}
    export LD_LIBRARY_PATH=${hookLibs}/lib:/run/opengl-driver/lib''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
    tool=$1
    shift
    case "$tool" in
      nsight-analyzer)
        exec python3 ${analyzerSrc}/plugins/nsight-graphics-analyzer/skills/nsight-graphics-analyzer/scripts/nsight.py "$@" ;;
      "")
        echo "usage: nsight-graphics <${lib.concatStringsSep "|" tools}> [args]" >&2
        exit 2 ;;
      *)
        exec ${hostDir}/"$tool" "$@" ;;
    esac
  '';
in
buildFHSEnv {
  pname = "nsight-graphics";
  inherit version;

  targetPkgs =
    pkgs:
    with pkgs;
    [
      zstd
      zlib
      stdenv.cc.cc.lib
      dbus
      glib
      fontconfig
      freetype
      libtiff
      libxkbcommon
      libGL
      libglvnd
      vulkan-loader
      wayland
      libx11
      libxcb
      libxcb-cursor
      libxcb-image
      libxcb-keysyms
      libxcb-render-util
      libxcb-util
      libxcb-wm
      libxext
      libxrender
      libxi
      libice
      libsm
      krb5
      nss
      nspr
      expat
      libdrm
      libpng
      python3
    ];

  runScript = dispatch;

  extraInstallCommands = ''
    for t in ${lib.concatStringsSep " " tools} ${lib.optionalString (analyzerSrc != null) "nsight-analyzer"}; do
      printf '#!/bin/sh\nexec %s/bin/nsight-graphics %s "$@"\n' "$out" "$t" > "$out/bin/$t"
      chmod +x "$out/bin/$t"
    done
  '';

  passthru = { inherit unwrapped hostDir; };

  meta = {
    description = "NVIDIA Nsight Graphics (FHS-wrapped) plus the nsight-graphics-analyzer CLI";
    homepage = "https://developer.nvidia.com/nsight-graphics";
    license = lib.licenses.unfree;
    platforms = [ "x86_64-linux" ];
    mainProgram = "nsight-analyzer";
  };
}
