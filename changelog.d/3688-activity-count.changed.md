- **Feed pill unread badge is now the DS `Count` primitive (#3688).** The Activity/Feed
  widget's hand-rolled `<span>` badge is replaced by `Count` from `@protolabsai/ui/primitives`,
  so it renders as `span.pl-count`. The alert-state `.activity-badge--alert` rule in
  `app/theme.css` colours it with the bare `var(--pl-color-status-error)` token (no hex
  fallback) and still wins over `.pl-count` at equal specificity. Text logic, the
  `data-alert="now"` blocked-page marker, and the unread/pending render condition are unchanged.
