import sys
import os
import socket
import time
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, 
                             QTextEdit, QGridLayout)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QTimer
from PyQt6.QtGui import QPixmap, QTransform, QPainter, QPen, QColor

class NetworkWorker(QThread):
    status_signal = pyqtSignal(str, bool)  # message, is_connected
    data_signal = pyqtSignal(str)          # incoming data from RT-21
    heading_signal = pyqtSignal(int)       # parsed heading

    def __init__(self, ip, port=6555):
        super().__init__()
        self.ip = ip
        self.port = int(port)
        self.running = False
        self.sock = None

    def run(self):
        self.running = True
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Increased timeout to 10 seconds to handle network latency/delays
            self.sock.settimeout(10.0) 
            self.status_signal.emit(f"Connecting to {self.ip}:{self.port}...", False)
            self.sock.connect((self.ip, self.port))
            self.status_signal.emit("Connected successfully!", True)
        except Exception as e:
            self.status_signal.emit(f"Connection failed: {str(e)}", False)
            self.running = False
            return

        buffer = ""
        while self.running:
            try:
                data = self.sock.recv(1024).decode('ascii', errors='ignore')
                if not data:
                    self.status_signal.emit("Connection closed by remote host.", False)
                    break
                
                buffer += data
                # Parse native DCU-1 messages ended by semicolons or newlines
                while ";" in buffer or "\n" in buffer:
                    if ";" in buffer:
                        cmd, buffer = buffer.split(";", 1)
                    else:
                        cmd, buffer = buffer.split("\n", 1)
                    
                    cmd = cmd.strip()
                    if cmd:
                        self.data_signal.emit(f"Received: {cmd}")
                        self.parse_rt21_response(cmd)
                        
            except socket.timeout:
                # Silently ignore read timeouts since we rely on the heartbeat loop now
                continue
            except Exception as e:
                if self.running:
                    self.status_signal.emit(f"Network error: {str(e)}", False)
                break

        self.disconnect_socket()

    def send_command(self, cmd_string):
        if self.sock and self.running:
            try:
                # Ensure correct format layout termination for RT-21
                if not cmd_string.endswith(';'):
                    cmd_string += ';'
                self.sock.sendall(cmd_string.encode('ascii'))
                self.data_signal.emit(f"Sent: {cmd_string}")
            except Exception as e:
                self.status_signal.emit(f"Send failed: {str(e)}", False)

    def parse_rt21_response(self, response):
        # Parses typical responses like 'AZ=245' or standard DCU headings
        if "AZ=" in response:
            try:
                heading = int(response.split("AZ=")[1].strip()[:3])
                self.heading_signal.emit(heading)
            except (ValueError, IndexError):
                pass
        elif response.isdigit():
            self.heading_signal.emit(int(response))

    def disconnect_socket(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None
        self.status_signal.emit("Disconnected.", False)

class CompassWidget(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(220, 220)
        self.current_heading = 0
        self.base_pixmap = QPixmap()
        
        # Look for any image file in the current working directory
        valid_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.bmp')
        found_image = None
        try:
            for file in os.listdir('.'):
                if file.lower().endswith(valid_extensions):
                    found_image = file
                    break
        except Exception:
            pass
                
        if found_image:
            self.base_pixmap.load(found_image)
        else:
            # Fallback placeholder if no image exists in the directory yet
            self.setText("Place compass rose image\nin this directory")
            self.setStyleSheet("border: 2px dashed #aaa; color: #777;")

    def set_heading(self, heading):
        self.current_heading = heading
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.base_pixmap.isNull():
            return
            
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Scale background image to fit the container area safely
        target_size = min(self.width(), self.height()) - 20
        scaled_pixmap = self.base_pixmap.scaled(target_size, target_size, 
                                                Qt.AspectRatioMode.KeepAspectRatio, 
                                                Qt.TransformationMode.SmoothTransformation)
        
        # Draw background image centered
        x = (self.width() - scaled_pixmap.width()) // 2
        y = (self.height() - scaled_pixmap.height()) // 2
        painter.drawPixmap(x, y, scaled_pixmap)
        
        # Draw dynamic rotated indicator needle pointing to current heading
        center_x = self.width() // 2
        center_y = self.height() // 2
        radius = target_size // 2 - 10
        
        painter.translate(center_x, center_y)
        painter.rotate(self.current_heading)
        
        # Draw elegant red pointing indicator needle
        pen = QPen(QColor(230, 0, 0), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(0, 0, 0, -radius)
        
        # Draw contrasting needle counter-weight tail
        pen.setColor(QColor(50, 50, 50))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawLine(0, 0, 0, 15)
        
        # Draw central physical pivot cap accent
        painter.setBrush(QColor(20, 20, 20))
        painter.drawEllipse(-5, -5, 10, 10)
        painter.end()

class RT21ControllerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RT-21 Network Controller")
        self.setGeometry(100, 100, 600, 400)
        self.worker = None
        self.init_ui()
        
        # 2-Second Keep-Alive Heartbeat Timer to prevent TCP line drops
        self.heartbeat_timer = QTimer()
        self.heartbeat_timer.timeout.connect(self.send_heartbeat)

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QHBoxLayout(main_widget)

        # Left Panel: Network Control & Commands
        left_panel = QVBoxLayout()
        
        # Connection Box Grid Layout Elements
        conn_layout = QGridLayout()
        conn_layout.addWidget(QLabel("IP Address:"), 0, 0)
        self.ip_input = QLineEdit("10.3.0.1") # Adjust to your controller default
        conn_layout.addWidget(self.ip_input, 0, 1)
        
        conn_layout.addWidget(QLabel("Port:"), 1, 0)
        self.port_input = QLineEdit("8080")
        conn_layout.addWidget(self.port_input, 1, 1)
        
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.toggle_connection)
        conn_layout.addWidget(self.connect_btn, 2, 0, 1, 2)
        left_panel.addLayout(conn_layout)

        # Status & Reading Display
        self.status_lbl = QLabel("Status: Disconnected")
        self.status_lbl.setStyleSheet("color: red; font-weight: bold;")
        left_panel.addWidget(self.status_lbl)
        
        self.heading_lbl = QLabel("Heading: ---°")
        self.heading_lbl.setStyleSheet("font-size: 20px; font-weight: bold; margin: 10px 0;")
        left_panel.addWidget(self.heading_lbl)

        # Direct Manual Control Target Layout Interface
        control_layout = QHBoxLayout()
        control_layout.addWidget(QLabel("Target Azimuth:"))
        self.target_input = QLineEdit()
        self.target_input.setPlaceholderText("0-359")
        control_layout.addWidget(self.target_input)
        
        self.send_btn = QPushButton("Slew")
        self.send_btn.setEnabled(False)
        self.send_btn.clicked.connect(self.send_heading)
        control_layout.addWidget(self.send_btn)
        left_panel.addLayout(control_layout)

        # Emergency Brake Control System Button Interrupter
        self.stop_btn = QPushButton("EMERGENCY STOP")
        self.stop_btn.setStyleSheet("background-color: #d9534f; color: white; font-weight: bold; padding: 8px;")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.emergency_stop)
        left_panel.addWidget(self.stop_btn)

        # Operational Console System Message Logs Container Frame Layout 
        left_panel.addWidget(QLabel("Console Log:"))
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        left_panel.addWidget(self.log_output)
        
        main_layout.addLayout(left_panel, stretch=3)

        # Right Panel: Live Visual Compass Layout Panel Holder Container
        right_panel = QVBoxLayout()
        self.compass = CompassWidget()
        right_panel.addWidget(self.compass)
        main_layout.addLayout(right_panel, stretch=2)

    def toggle_connection(self):
        if self.worker and self.worker.isRunning():
            self.heartbeat_timer.stop()
            self.worker.disconnect_socket()
            self.worker.wait()
        else:
            ip = self.ip_input.text().strip()
            port = self.port_input.text().strip()
            self.worker = NetworkWorker(ip, port)
            self.worker.status_signal.connect(self.update_status)
            self.worker.data_signal.connect(self.log_message)
            self.worker.heading_signal.connect(self.update_heading)
            self.worker.start()

    def update_status(self, msg, is_connected):
        self.status_lbl.setText(f"Status: {msg}")
        if is_connected:
            self.status_lbl.setStyleSheet("color: green; font-weight: bold;")
            self.connect_btn.setText("Disconnect")
            self.send_btn.setEnabled(True)
            self.stop_btn.setEnabled(True)
            # Begin query heartbeat interval when network link goes live cleanly
            self.heartbeat_timer.start(2000) 
        else:
            self.status_lbl.setStyleSheet("color: red; font-weight: bold;")
            self.connect_btn.setText("Connect")
            self.send_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            self.heartbeat_timer.stop()

    def send_heading(self):
        target = self.target_input.text().strip()
        if target.isdigit() and 0 <= int(target) <= 359:
            # Format explicitly to standard extension string syntax rule: AP1AM3;
            padded_target = target.zfill(3)
            self.worker.send_command(f"AP1AM{padded_target};")
        else:
            self.log_message("System Error: Please enter a valid azimuth angle (0-359).")

    def emergency_stop(self):
        # Native break execution parameter command overrides slewing instantly
        self.worker.send_command("A;") 

    def send_heartbeat(self):
        # Asynchronously polling status targets over network prevents timeouts
        if self.worker and self.worker.running:
            self.worker.send_command("AM1;") 

    def update_heading(self, heading):
        self.heading_lbl.setText(f"Heading: {heading}°")
        self.compass.set_heading(heading)

    def log_message(self, msg):
        self.log_output.append(msg)

    def closeEvent(self, event):
        self.heartbeat_timer.stop()
        if self.worker and self.worker.isRunning():
            self.worker.disconnect_socket()
            self.worker.wait()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = RT21ControllerApp()
    window.show()
    sys.exit(app.exec())
