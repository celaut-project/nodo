import requests
import psutil
import time
from typing import Dict, Generator

from src.database.sql_connection import SQLConnection
from src.utils.env import EnvManager
from src.utils.logger import LOGGER as log

env_manager = EnvManager()
sc = SQLConnection()

# Database Configuration
DATABASE_FILE = env_manager.get_env("DATABASE_FILE")

# API Configuration
ENERGY_API_URL = env_manager.get_env("ENERGY_API_URL")
ENERGY_API_KEY = env_manager.get_env("ENERGY_API_KEY")

# Monitoring Configuration
DEFAULT_MONITOR_INTERVAL = int(env_manager.get_env("MONITOR_INTERVAL"))
DEFAULT_POWER_RATE = float(env_manager.get_env("DEFAULT_POWER_RATE"))

# Hardware Limits
MAX_POWER_CONSUMPTION = float(env_manager.get_env("MAX_POWER_CONSUMPTION"))
MIN_MEMORY_LIMIT = int(env_manager.get_env("MIN_MEMORY_LIMIT"))


class EnergyCostMonitor:
    def __init__(self):
        """Initialize the energy and cost monitor"""
        self.setup_logging()
        self.db = sc

    def get_current_energy_price(self) -> float:
        """Get current energy price from API"""
        headers = {'Authorization': f'Bearer {ENERGY_API_KEY}'} if ENERGY_API_KEY else {}
        
        try:
            response = requests.get(ENERGY_API_URL, headers=headers)
            response.raise_for_status()
            return response.json()['price_per_kwh']
        except requests.RequestException as e:
            return DEFAULT_POWER_RATE

    def get_system_metrics(self) -> Dict:
        """Get current system metrics"""
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        
        # Calculate power consumption based on CPU usage and configured maximum
        estimated_power = (cpu_percent / 100) * MAX_POWER_CONSUMPTION
        
        return {
            'cpu_percent': cpu_percent,
            'memory_usage': memory.percent,
            'power_consumption': estimated_power
        }

    def calculate_cost(self, power_consumption: float, price_per_kwh: float) -> float:
        """Calculate cost based on power consumption"""
        # Convert watts to kilowatts and calculate hourly cost
        return (power_consumption / 1000) * price_per_kwh

    def record_metrics(self):
        """Record current metrics to database"""
        try:
            metrics = self.get_system_metrics()
            current_price = self.get_current_energy_price()
            cost = self.calculate_cost(metrics['power_consumption'], current_price)
            
            self.db.insert_energy_record(
                cpu_percent=metrics['cpu_percent'],
                memory_usage=metrics['memory_usage'],
                power_consumption=metrics['power_consumption'],
                cost=cost
            )
            
            log(
                f"Metrics recorded: CPU: {metrics['cpu_percent']}%, "
                f"Memory: {metrics['memory_usage']}%, "
                f"Power: {metrics['power_consumption']:.2f}W, "
                f"Cost: {cost:.4f}€/h"
            )
                           
        except Exception as e:
            log(f"Error recording metrics: {e}")

    def monitor(self, interval: int = None):
        """Start continuous monitoring"""
        if interval is None:
            interval = DEFAULT_MONITOR_INTERVAL
            
        log(f"Starting monitoring every {interval} seconds...")
        try:
            while True:
                self.record_metrics()
                time.sleep(interval)
        except KeyboardInterrupt:
            log("Monitoring stopped by user")
            

    def get_historical_data(self, limit: int = 100) -> Generator[Dict, None, None]:
        """Retrieve historical monitoring data"""
        return self.db.get_latest_energy_records(limit)
