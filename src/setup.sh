#!/usr/bin/env bash
set -euv

echo "Making sure your environment is correctly setup"

if [[ -f /etc/NIXOS ]]; then
    if [[ -z ${NIXSHELL:=} ]]; then
        echo "Please run nix-shell to ensure your setup is correct"
        exit 1
    else
        continue
    fi
else
    opam init -a --compiler=4.07.1
    eval `opam config env`
    opam update
    # For Coq:
    opam pin add coq 8.11.0
    opam pin -y add menhir 20190626
    # For SerAPI:
    opam install -y coq-serapi
    pip3 install --user -r requirements.txt
    make src/dataloader.so
fi

function check-and-clone {
    if [[ ! -d $1 ]]; then
        git clone $2
    fi
    (cd $1 && git fetch && git checkout $3) || exit 1
}
function setup-compcert {
    check-and-clone\
        "CompCert" "https://github.com/AbsInt/CompCert.git"\
        "f047fcb7852ff58c0c62f10d41f91f3f88552780"
    (
        set -euv
        cd CompCert
        if [[ ! -f "Makefile.config" ]]; then
            ./configure x86_64-linux
        fi
        make -j `nproc`
        ../src/patch_compcert.sh
    ) || exit 1
}

setup-compcert
