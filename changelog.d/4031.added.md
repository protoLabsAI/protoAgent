- **A path setting can hold a list of folders (`multiple: true`).**
  A `type: path` setting — core or plugin manifest `settings:` — that declares `multiple: true`
  now renders one row per folder, each with its own **Browse…**, a Remove (×) button, and an
  **Add folder** button that appends a row and opens Browse… for it straight away. Before,
  Browse… replaced the whole value, so a field like the data plugin's **Data folders** could
  only be edited as comma-separated text. The saved value is still one string (the rows joined
  with newlines, duplicates dropped), so existing configs and older cores keep working; an
  existing comma-separated value loads as rows. A bundle's `config_inputs` `type: path` honours
  `multiple: true` in the New agent set-up step too.
