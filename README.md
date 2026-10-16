# BMR-Com · Cotton On-Call

**Weekly CFTC Cotton On-Call dashboard with a BMR-Com desk brief.**

🔗 **Live:** https://bmr-com.github.io/cotton-on-call/

- **Weekly On-Call Brief:** a written read of the latest report (unfixed call sales and purchases, front-month fixation, spreads, positioning and trade flow), generated weekly from the latest on-call, COT, USDA and price data (`intel/weekly_brief.py`).
- **On-call dashboard:** every unfixed call sales and purchases position by contract month since 2004, compared with history, updated each Thursday from the CFTC release (`scrape.py`, GitHub Actions).

Call cotton is physical cotton bought or sold at a price to be fixed later against an ICE futures month. Unfixed call **sales** (mills still to fix) are latent futures buying; unfixed call **purchases** (producers still to fix) are latent futures selling.

---
*BMR-Com · independent cotton research. Data: CFTC Cotton On-Call. Not investment advice.*
