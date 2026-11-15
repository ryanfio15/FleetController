# Thin delegation to ./fc.sh, which is the real entrypoint (make is not
# present on a default Windows install, so fc.sh has to work standalone).
.PHONY: up down nuke ps logs verify certs test scale fault psql help \
        k8s-up k8s-down k8s-deps k8s-build k8s-load k8s-apply k8s-status \
        k8s-verify k8s-psql k8s-scale

up:      ; @./fc.sh up
down:    ; @./fc.sh down
nuke:    ; @./fc.sh nuke
ps:      ; @./fc.sh ps
logs:    ; @./fc.sh logs
verify:  ; @./fc.sh verify
certs:   ; @./fc.sh certs
test:    ; @./fc.sh test
psql:    ; @./fc.sh psql
help:    ; @./fc.sh help

# make scale N=50   /   make fault KIND=overheat
scale:   ; @./fc.sh scale $(N)
fault:   ; @./fc.sh fault $(KIND)

# Kubernetes (Stage 2). K8S_OVERLAY=fabric targets the FABRIC cluster.
k8s-up:     ; @./fc.sh k8s-up
k8s-down:   ; @./fc.sh k8s-down
k8s-deps:   ; @./fc.sh k8s-deps
k8s-build:  ; @./fc.sh k8s-build
k8s-load:   ; @./fc.sh k8s-load
k8s-apply:  ; @./fc.sh k8s-apply
k8s-status: ; @./fc.sh k8s-status
k8s-verify: ; @./fc.sh k8s-verify
k8s-psql:   ; @./fc.sh k8s-psql
k8s-scale:  ; @./fc.sh k8s-scale $(N)
