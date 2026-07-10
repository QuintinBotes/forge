# versions.tf — toolchain + provider pins for the prod environment root.
#
# infra/versions.tf documents the repo-wide pins; this file is the actual
# `required_providers` block Tofu resolves against, because infra/envs/dev
# is a standalone root module (it is what you `cd` into and `init`/`plan`/
# `apply`), not a child of infra/. The version constraints mirror
# infra/versions.tf exactly — keep them in sync if either changes.

terraform {
  required_version = ">= 1.6"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.48"
    }

    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.40"
    }
  }
}
