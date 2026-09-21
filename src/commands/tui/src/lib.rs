/// The CELL page: policy levers and profiles.
pub mod cell;

/// The SCHEDULE page: the hours this node works, and the arithmetic of a window that
/// runs through midnight.
pub mod schedule;

/// The ENERGY page: what the machine costs to run, and where that figure may come
/// from (issue #395).
pub mod energy;

/// Application.
pub mod app;

/// Terminal events handler.
pub mod event;

/// Widget renderer.
pub mod ui;

/// Terminal user interface.
pub mod tui;

/// Event handler.
pub mod handler;
