#!/usr/bin/env python3
"""
Telemetry Leaderboard module for MeshCore Bot
Tracks extreme network metrics and maintains a "hall of fame" of notable achievements
"""

import pickle
import time
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime


class TelemetryLeaderboard:
    """Tracks extreme network metrics across all mesh nodes.
    
    Monitors battery levels, temperature, speed, altitude, signal quality,
    and other telemetry metrics to maintain records of notable achievements.
    """
    
    # Leaderboard metrics
    METRICS = {
        'lowestBattery': {'name': 'Low Battery', 'emoji': '🪫', 'unit': '%', 'init_value': 100},
        'longestUptime': {'name': 'Uptime', 'emoji': '🕰️', 'unit': 'days', 'init_value': 0},
        'fastestSpeed': {'name': 'Speed', 'emoji': '🚓', 'unit': 'km/h', 'init_value': 0},
        'fastestAirSpeed': {'name': 'Air Speed', 'emoji': '🚀', 'unit': 'km/h', 'init_value': 0},
        'highestAltitude': {'name': 'Altitude', 'emoji': '⛰️', 'unit': 'm', 'init_value': 0},
        'tallestNode': {'name': 'Height', 'emoji': '📏', 'unit': 'm', 'init_value': 0},
        'coldestTemp': {'name': 'Coldest', 'emoji': '🥶', 'unit': '°C', 'init_value': 1000},
        'hottestTemp': {'name': 'Hottest', 'emoji': '🔥', 'unit': '°C', 'init_value': -1000},
        'worstAirQuality': {'name': 'Air Quality', 'emoji': '💨', 'unit': 'IAQ', 'init_value': 0},
        'bestSignal': {'name': 'Best RF', 'emoji': '📶', 'unit': 'dBm', 'init_value': -1000},
        'worstSignal': {'name': 'Worst RF', 'emoji': '📉', 'unit': 'dBm', 'init_value': 1000},
        'mostMessages': {'name': 'Most Messages', 'emoji': '💬', 'unit': 'msgs', 'init_value': 0},
        'mostTelemetry': {'name': 'Most Telemetry', 'emoji': '📊', 'unit': 'packets', 'init_value': 0},
        'mostPaxWiFi': {'name': 'WiFi Devices', 'emoji': '📡', 'unit': 'count', 'init_value': 0},
        'mostPaxBLE': {'name': 'BLE Devices', 'emoji': '🔵', 'unit': 'count', 'init_value': 0},
        'farthestNode': {'name': 'Farthest Distance', 'emoji': '📍', 'unit': 'km', 'init_value': 0},
        'northernMost': {'name': 'Most Northern', 'emoji': '🧭', 'unit': 'lat', 'init_value': -90},
        'southernMost': {'name': 'Most Southern', 'emoji': '🧭', 'unit': 'lat', 'init_value': 90},
    }
    
    def __init__(self, data_dir: str = "data/telemetry", logger: Optional[logging.Logger] = None):
        """Initialize the telemetry leaderboard.
        
        Args:
            data_dir: Directory for storing leaderboard data
            logger: Logger instance for logging events
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.leaderboard_file = self.data_dir / "leaderboard.pkl"
        self.logger = logger or logging.getLogger(__name__)
        
        # Initialize leaderboard structure
        self.leaderboard = {}
        self.node_message_counts = {}
        self.node_telemetry_counts = {}
        self.recent_special_packets = {
            'admin': [],
            'tunnel': [],
            'audio': [],
            'simulator': []
        }
        
        self._initialize_leaderboard()
        self.load_leaderboard()
    
    def _initialize_leaderboard(self):
        """Initialize empty leaderboard with default values."""
        for metric_key, metric_info in self.METRICS.items():
            self.leaderboard[metric_key] = {
                'nodeID': None,
                'nodeName': None,
                'value': metric_info['init_value'],
                'timestamp': None
            }
    
    def reset(self):
        """Reset the leaderboard to initial state."""
        self._initialize_leaderboard()
        self.node_message_counts = {}
        self.node_telemetry_counts = {}
        self.recent_special_packets = {k: [] for k in self.recent_special_packets.keys()}
        self.save_leaderboard()
        self.logger.info("Telemetry leaderboard has been reset")
    
    def update_metric(self, metric_key: str, node_id: int, node_name: str, value: float,
                     timestamp: Optional[float] = None, check_record: bool = True) -> bool:
        """Update a leaderboard metric if it's a new record.
        
        Args:
            metric_key: Key of the metric to update
            node_id: ID of the node recording the metric
            node_name: Name/identifier of the node
            value: The metric value
            timestamp: Timestamp of the metric (defaults to now)
            check_record: Whether to validate if this is actually a record
            
        Returns:
            bool: True if this is a new record, False otherwise
        """
        if metric_key not in self.METRICS:
            self.logger.warning(f"Unknown metric: {metric_key}")
            return False
        
        timestamp = timestamp or time.time()
        current_record = self.leaderboard[metric_key]['value']
        metric_info = self.METRICS[metric_key]
        
        # Check if this is a new record based on metric type
        is_new_record = False
        if self.leaderboard[metric_key]['nodeID'] is None:
            # First record
            is_new_record = True
        elif 'lowest' in metric_key or 'worst' in metric_key:
            is_new_record = value < current_record
        else:
            is_new_record = value > current_record
        
        if is_new_record or not check_record:
            self.leaderboard[metric_key] = {
                'nodeID': node_id,
                'nodeName': node_name,
                'value': value,
                'timestamp': timestamp
            }
            self.logger.info(
                f"🏆 New record! {metric_info['name']}: {value:.1f}{metric_info['unit']} "
                f"from {node_name}"
            )
            return True
        
        return False
    
    def track_message(self, node_id: int, node_name: str) -> None:
        """Track a message from a node.
        
        Args:
            node_id: ID of the sending node
            node_name: Name of the sending node
        """
        if node_id not in self.node_message_counts:
            self.node_message_counts[node_id] = {'name': node_name, 'count': 0}
        
        self.node_message_counts[node_id]['count'] += 1
        
        # Update "most messages" record
        max_node_id = None
        max_count = 0
        for nid, data in self.node_message_counts.items():
            if data['count'] > max_count:
                max_count = data['count']
                max_node_id = nid
        
        if max_node_id:
            self.update_metric(
                'mostMessages',
                max_node_id,
                self.node_message_counts[max_node_id]['name'],
                max_count,
                check_record=False
            )
    
    def track_telemetry(self, node_id: int, node_name: str) -> None:
        """Track a telemetry packet from a node.
        
        Args:
            node_id: ID of the sending node
            node_name: Name of the sending node
        """
        if node_id not in self.node_telemetry_counts:
            self.node_telemetry_counts[node_id] = {'name': node_name, 'count': 0}
        
        self.node_telemetry_counts[node_id]['count'] += 1
        
        # Update "most telemetry" record
        max_node_id = None
        max_count = 0
        for nid, data in self.node_telemetry_counts.items():
            if data['count'] > max_count:
                max_count = data['count']
                max_node_id = nid
        
        if max_node_id:
            self.update_metric(
                'mostTelemetry',
                max_node_id,
                self.node_telemetry_counts[max_node_id]['name'],
                max_count,
                check_record=False
            )
    
    def track_special_packet(self, packet_type: str, node_id: int, node_name: str) -> None:
        """Track a special packet type (admin, tunnel, audio, simulator).
        
        Args:
            packet_type: Type of packet ('admin', 'tunnel', 'audio', 'simulator')
            node_id: ID of the node
            node_name: Name of the node
        """
        if packet_type in self.recent_special_packets:
            self.recent_special_packets[packet_type].append({
                'nodeID': node_id,
                'nodeName': node_name,
                'timestamp': time.time()
            })
            # Keep only last 10 of each type
            if len(self.recent_special_packets[packet_type]) > 10:
                self.recent_special_packets[packet_type].pop(0)
    
    def format_leaderboard(self) -> str:
        """Format the leaderboard as a readable string.
        
        Returns:
            str: Formatted leaderboard display
        """
        lines = ["📊 **Telemetry Leaderboard** 📊\n"]
        
        for metric_key, metric_info in self.METRICS.items():
            record = self.leaderboard[metric_key]
            
            if record['nodeID'] is None:
                lines.append(f"{metric_info['emoji']} {metric_info['name']}: No records yet")
            else:
                value_str = f"{record['value']:.1f}" if isinstance(record['value'], float) else str(record['value'])
                lines.append(
                    f"{metric_info['emoji']} {metric_info['name']}: "
                    f"{value_str}{metric_info['unit']} - {record['nodeName']}"
                )
        
        # Add special packets section if any
        if any(self.recent_special_packets.values()):
            lines.append("\n📡 **Special Packets** 📡")
            for ptype, packets in self.recent_special_packets.items():
                if packets:
                    last_packet = packets[-1]
                    lines.append(f"  {ptype.capitalize()}: {last_packet['nodeName']}")
        
        return "\n".join(lines)
    
    def save_leaderboard(self) -> None:
        """Save leaderboard to pickle file."""
        try:
            data = {
                'leaderboard': self.leaderboard,
                'node_message_counts': self.node_message_counts,
                'node_telemetry_counts': self.node_telemetry_counts,
                'recent_special_packets': self.recent_special_packets,
                'saved_at': time.time()
            }
            with open(self.leaderboard_file, 'wb') as f:
                pickle.dump(data, f)
            self.logger.debug(f"Telemetry leaderboard saved to {self.leaderboard_file}")
        except Exception as e:
            self.logger.error(f"Failed to save leaderboard: {e}")
    
    def load_leaderboard(self) -> None:
        """Load leaderboard from pickle file if it exists."""
        if not self.leaderboard_file.exists():
            self.logger.info("No existing leaderboard found, starting fresh")
            return
        
        try:
            with open(self.leaderboard_file, 'rb') as f:
                data = pickle.load(f)
            
            self.leaderboard = data.get('leaderboard', self.leaderboard)
            self.node_message_counts = data.get('node_message_counts', {})
            self.node_telemetry_counts = data.get('node_telemetry_counts', {})
            self.recent_special_packets = data.get('recent_special_packets', self.recent_special_packets)
            
            self.logger.info(f"Loaded existing leaderboard from {self.leaderboard_file}")
        except Exception as e:
            self.logger.error(f"Failed to load leaderboard: {e}")
            self._initialize_leaderboard()
