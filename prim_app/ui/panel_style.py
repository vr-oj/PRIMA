"""Compact control-card styling, following BURST's acquisition panels."""
PANEL_STYLE = '''
QFrame[panel="true"] { background: #30353c; border: 1px solid #49515b; border-radius: 9px; }
QLabel[role="title"] { font-size: 13px; font-weight: 600; color: #edf1f5; }
QLabel[role="muted"] { font-size: 11px; color: #abb6c3; }
QLabel[role="metric"] { font-size: 18px; font-weight: 600; font-family: Consolas; color: #edf1f5; }
QLabel[role="warning"] { color: #f0c66c; }
QPushButton[role="record"] { background: #327a8e; font-weight: 600; padding: 5px 8px; }
QPushButton[role="record"]:disabled { background: #424b55; color: #a4adb8; }
'''
