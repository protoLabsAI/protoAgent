// The seven built-in Work sections (#4955 §5). Each module registers its list/detail
// renderer into the Work view on import via `registerWorkSection` — the import IS the wiring,
// so pulling in this barrel once (from App's shell) guarantees every core section is live and
// none of them renders the "unregistered" Empty. Ordered as the Work view lays them out.
// Activity is no longer one of them (S4c): the feed lives outside Work, on the utility-bar
// Feed pill's own dialog (`src/activity/ActivityWidget.tsx`).
//
// WorkView itself still imports the handful of sections its own tests exercise directly, so
// importing WorkView alone keeps those registered; this barrel adds the rest for production.
import "./decisions";
import "./goals";
import "./watches";
import "./tasks";
import "./schedules";
import "./background";
import "./directives";
