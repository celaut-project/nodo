/// What the node needs its operator to *do*: the gateway port's firewall rule and
/// a missing Java runtime, surfaced where an operator actually looks.
pub mod alerts;

/// Colour themes. Every colour `ui` draws comes from here; the default matches the
/// Ubuntu terminal this node is overwhelmingly installed from.
pub mod theme;

/// The CELL page: policy levers and profiles.
pub mod cell;

/// The SCHEDULE page: the hours this node works, and the arithmetic of a window that
/// runs through midnight.
pub mod schedule;

/// The ENERGY page: what the machine costs to run, and where that figure may come
/// from (issue #395).
pub mod energy;

/// The PEERS page: other nodes this one has introduced itself to or heard from.
pub mod peers;

/// The CLIENTS page: who this node's own gateway has issued a client_id to.
pub mod clients;

/// The CHAT page: free-text conversations with peer operators (issue #431).
pub mod chat;

/// Right-click menus: an element's actions, as the keys that already do them.
pub mod context_menu;

/// The DOCS page: the installation's `docs/` folder, indexed and rendered.
pub mod docs;

/// Application.
pub mod app;

/// Terminal events handler.
pub mod event;

/// Fitting pages, tables and popups to the terminal's size (issue #453).
pub mod layout_util;

/// Widget renderer.
pub mod ui;

/// Terminal user interface.
pub mod tui;

/// Event handler.
pub mod handler;

/// Every page and popup drawn at a grid of terminal sizes (issue #453).
#[cfg(test)]
mod responsive_sweep;
