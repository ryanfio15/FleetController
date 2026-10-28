# Thin delegation to ./fc.sh, which is the real entrypoint (make is not
# present on a default Windows install, so fc.sh has to work standalone).
.PHONY: up down nuke ps logs verify certs test scale fault psql help

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
