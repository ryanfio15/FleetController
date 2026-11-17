# Build context is the repository root, like every other service here, so that
# `docker build -f services/<svc>/Dockerfile .` works uniformly in fc.sh and
# CI. This one has nothing to copy from libs/ — there is no build step at all,
# which is the point: the three files in public/ are the shipped artefact.
FROM nginx:1.27-alpine

COPY services/web-ui/nginx.conf /etc/nginx/conf.d/default.conf
COPY services/web-ui/public /usr/share/nginx/html

EXPOSE 8080
