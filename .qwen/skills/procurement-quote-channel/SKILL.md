# Procurement Quote Channel Extractor

Use when extracting how a buyer can request a quotation from a supplier page.

Return only evidence-backed channels:
- `rfq_form` for request-quote, quotation, RFQ, or enquiry forms.
- `contact_email` for role-based business emails such as sales@, info@, enquiries@.
- `phone` for business phone numbers.
- `contact_page` for contact/enquiry pages.
- `rate_card` for public rate-card or price-list links.
- `portal_login_required` when quotation or pricing is hidden behind login.

Do not infer a quote channel from generic marketing copy. Include exact page-text spans whenever the channel appears in fetched text.
