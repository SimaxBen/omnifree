# Waiting for a working card

Work that is planned but blocked until a payment card can be verified (no spending: every item stays
inside an Always-Free tier). The provider sign-ups themselves are tracked in `PROVISIONING_STATUS.md`
and in the local PDF `OmniFree-avec-carte.pdf`.

## Stirling PDF (requested 2026-10-20)

Self-host [Stirling PDF](https://github.com/Stirling-Tools/Stirling-PDF) and use it from the OmniFree web app.

- **Why it waits:** it needs a Docker host with 1 to 2 GB of RAM. Every free host that fits asks for a card
  (Oracle, Cloud Run, Render, Fly), and Hugging Face Docker Spaces now need PRO. The Vercel hub cannot run it.
- **Where:** the Oracle Always-Free ARM VM (Madrid, `eu-madrid-1`), next to FreeLLMAPI.
- **Plan:**
  1. Run the `stirlingtools/stirling-pdf` container on the Oracle VM with login enabled and an API key.
     Bind it to localhost or the Tailscale address only, not to the public internet.
  2. Store `STIRLING_PDF_URL` and `STIRLING_PDF_API_KEY` in the vault and push them to the hub.
  3. Add a `/pdf/*` hub route that proxies the main operations (merge, split, compress, convert to and from
     PDF, OCR, rotate, watermark, sign, page numbers). The route reads input files from the Drive and
     writes results back to `pdf/<yyyy>/<mm>/`. Big files go through presigned B2 URLs, because Vercel
     caps request bodies at about 4.5 MB.
  4. Add a "PDF tools" page in the PWA (More > PDF tools, plus a "PDF tools" action on PDF files in Drive)
     and matching MCP tools (`pdf_merge`, `pdf_compress`, `pdf_ocr`, ...).
  5. Add a health check to the uptime workflow and to the free-tier budget.
- **Fallback:** if the card stays blocked, run the same container on the home PC and expose it with
  Tailscale Funnel. PDF tools then work only while the PC is on.
