# Defense Audit Report

- Run ID: `audit_20260509_061139`
- Captured At: `2026-05-08T22:11:53.342777+00:00`
- API Count: **69**

## Findings

- **[low] missing_csp_meta** @ https://www.kampojanechen.org/
  - Field: `CSP`
  - Description: No CSP meta tag detected.
  - Recommendation: Set strict CSP in HTTP headers.
  - Evidence: `meta not found`

- **[low] internal_id_exposure** @ https://track.91app.io/v2/did
  - Field: `id`
  - Description: JSON appears to expose id-like fields.
  - Recommendation: Minimize internal identifiers when unnecessary.
  - Evidence: `did`

- **[low] missing_csp_meta** @ https://www.kampojanechen.org/v2/Official/NewestSalePage
  - Field: `CSP`
  - Description: No CSP meta tag detected.
  - Recommendation: Set strict CSP in HTTP headers.
  - Evidence: `meta not found`

- **[info] crawler_policy** @ https://www.kampojanechen.org/robots.txt
  - Field: `/robots.txt`
  - Description: /robots.txt checked.
  - Recommendation: Ensure policy reflects intended crawling visibility.
  - Evidence: `status=200`

- **[info] crawler_policy** @ https://www.kampojanechen.org/v2/Official/NewestSalePage/robots.txt
  - Field: `/robots.txt`
  - Description: /robots.txt checked.
  - Recommendation: Ensure policy reflects intended crawling visibility.
  - Evidence: `status=404`

- **[info] crawler_policy** @ https://www.kampojanechen.org/sitemap.xml
  - Field: `/sitemap.xml`
  - Description: /sitemap.xml checked.
  - Recommendation: Ensure policy reflects intended crawling visibility.
  - Evidence: `status=404`

- **[info] crawler_policy** @ https://www.kampojanechen.org/v2/Official/NewestSalePage/sitemap.xml
  - Field: `/sitemap.xml`
  - Description: /sitemap.xml checked.
  - Recommendation: Ensure policy reflects intended crawling visibility.
  - Evidence: `status=404`
