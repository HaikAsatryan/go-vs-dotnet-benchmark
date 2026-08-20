# services/dotnet/Dockerfile: Ledgerline .NET SUT.
# SDK build -> chiseled aspnet runtime. Image digests pinned 2026-08-13.

FROM mcr.microsoft.com/dotnet/sdk:10.0@sha256:e1fc6e423f543119c406d24e2e687d67c569f18f04a37a8b0005d80ad0dcee80 AS build
WORKDIR /src

# Restore against the csproj only first (layer cache). The test project lives in a subfolder and
# is excluded from the web app compile (csproj Compile Remove), so it never enters the image.
COPY Ledgerline.csproj ./
RUN dotnet restore Ledgerline.csproj

COPY . .
# Compiled EF model + STJ source-gen are committed; no codegen toolchain needed at build.
RUN dotnet publish Ledgerline.csproj -c Release -o /app --no-restore
# Pre-owned pdf-stub dir (see services/go/Dockerfile): chiseled has no shell,
# so the dir is staged here and COPY --chown'd to the app uid (1654).
RUN mkdir -p /staging/data/pdf

FROM mcr.microsoft.com/dotnet/aspnet:10.0-noble-chiseled@sha256:0839314d08bb65da369135389a5d8291f75ace587fbb0488f469eb92c62eef68
WORKDIR /app
COPY --from=build --chown=1654:1654 /staging/data /data
COPY --from=build /app .

# Non-root (chiseled images set APP_UID). PDF volume mount target is /data/pdf (PDF_DIR default).
# No ASPNETCORE_HTTP_PORTS: Program.cs UseUrls binds 0.0.0.0:{PORT} authoritatively.
USER $APP_UID
EXPOSE 8080
ENTRYPOINT ["./Ledgerline"]
