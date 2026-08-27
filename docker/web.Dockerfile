# syntax=docker/dockerfile:1

# `--platform=$BUILDPLATFORM` pins this stage to the *builder's* architecture.
#
# Without it, a multi-arch build runs `npm ci` and `vite build` under QEMU
# emulation once per target architecture, which took over twenty-five minutes
# for the arm64 leg and dominated the release. The output is static files that
# are byte-identical either way, so the emulation bought nothing at all.
#
# Only the final nginx stage is architecture-specific.
FROM --platform=$BUILDPLATFORM node:22-alpine AS build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY web/ ./
# Baked in at build time, so it has to be present here rather than at run time.
# Empty means same-origin, which is what nginx serves.
ARG VITE_API_BASE=""
ENV VITE_API_BASE=$VITE_API_BASE
RUN npm run build

FROM nginx:1.29-alpine
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 80
