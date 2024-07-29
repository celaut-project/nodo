## Simple Async template

This simple async template will create the following project structure:

```text
src/
├── app.rs     -> holds the state and application logic
├── event.rs   -> handles the terminal events (key press, mouse click, resize, etc.)
├── handler.rs -> handles the key press events and updates the application
├── lib.rs     -> module definitions
├── main.rs    -> entry-point
├── tui.rs     -> initializes/exits the terminal interface
└── ui.rs      -> renders the widgets / UI
```

This is identical to the [simple] template but has `async` events out of the box with `tokio` and
`crossterm`'s `EventStream`.
