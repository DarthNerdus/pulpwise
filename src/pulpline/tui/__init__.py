"""Textual-based TUI for pulpline.

Tab-cycle layout, one view per tab. Adding a new view = drop a module in
`tui/views/`, register it in `tui/views/__init__.VIEWS`. The app builds tab
panes from that registry.
"""
