# services/go/Dockerfile: Ledgerline Go SUT.
# Multi-stage: golang build -> distroless/static-debian13:nonroot.
# Image digests pinned 2026-08-13.

FROM golang:1.26.5-bookworm@sha256:53eeac89074db483fdf0ab3be1df32bf6e47562263d2d0d6baa7f26acb4957dd AS build
WORKDIR /src

# Module cache layer: copy manifests first for build-cache reuse.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
# Static, stripped, reproducible. sqlc output is committed, so no codegen here.
RUN CGO_ENABLED=0 GOOS=linux go build \
    -trimpath \
    -ldflags='-w -s' \
    -o /out/server ./cmd/server
# Pre-owned pdf-stub dir: a named volume mounted at /data/pdf initializes from
# this image path (ownership included), so the nonroot runtime can write to it.
RUN mkdir -p /staging/data/pdf

FROM gcr.io/distroless/static-debian13:nonroot@sha256:f7f8f729987ad0fdf6b05eeeae94b26e6a0f613bdf46feea7fc40f7bd72953e6
COPY --from=build --chown=65532:65532 /staging/data /data
COPY --from=build /out/server /server
USER nonroot
EXPOSE 8080
ENTRYPOINT ["/server"]
