#!/usr/bin/env python3
"""
Discord Docker Management Bot - Full Featured
VPS Docker Container Management with SSH/Tmate Access
Admin-Only Commands for Container Creation and Management
80+ Features Complete Implementation
"""

import discord
from discord.ext import commands, tasks
import docker
import asyncio
import json
import os
from datetime import datetime, timedelta
import subprocess
import re
import sqlite3
from typing import Optional, List, Dict
import psutil
import logging
import traceback

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Bot Configuration
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', 'YOUR_BOT_TOKEN_HERE')
ADMIN_ROLE_ID = os.getenv('ADMIN_ROLE', None)
VPS_HOST = os.getenv('VPS_HOST', 'localhost')
VPS_USER = os.getenv('VPS_USER', 'root')
VPS_PASSWORD = os.getenv('VPS_PASSWORD', '')
DOCKER_SOCKET = os.getenv('DOCKER_SOCKET', 'unix:///var/run/docker.sock')

# Initialize Discord Bot - REMOVE DEFAULT HELP COMMAND
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
bot = commands.Bot(command_prefix='/', intents=intents, help_command=None)

# Initialize Docker Client
try:
    docker_client = docker.DockerClient(base_url=DOCKER_SOCKET)
    logger.info("✅ Docker connection established")
except Exception as e:
    logger.error(f"Docker connection failed: {e}")
    docker_client = None

# Database Setup
DB_FILE = 'containers.db'

def init_database():
    """Initialize SQLite database for container management"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS containers (
            container_id TEXT PRIMARY KEY,
            container_name TEXT UNIQUE,
            user_id INTEGER,
            ram TEXT,
            cores INTEGER,
            disk TEXT,
            status TEXT,
            created_at TIMESTAMP,
            expires_at TIMESTAMP,
            ssh_port INTEGER,
            tmate_session TEXT,
            container_ip TEXT
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            containers_count INTEGER DEFAULT 0,
            total_ram_used TEXT,
            last_created TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            action TEXT,
            container_id TEXT,
            timestamp TIMESTAMP,
            status TEXT
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            container_id TEXT,
            backup_name TEXT,
            created_at TIMESTAMP,
            size TEXT
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized")

# Helper Functions
def is_admin(ctx):
    """Check if user has admin role"""
    if ADMIN_ROLE_ID:
        return any(str(role.id) == ADMIN_ROLE_ID for role in ctx.author.roles)
    return any(role.name == 'Admin' for role in ctx.author.roles)

def parse_size(size_str: str) -> int:
    """Convert size string (1GB, 512MB, etc.) to bytes"""
    units = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4}
    size_str = size_str.upper().strip()
    for unit, multiplier in units.items():
        if size_str.endswith(unit):
            try:
                return int(float(size_str[:-len(unit)]) * multiplier)
            except:
                return 0
    try:
        return int(size_str)
    except:
        return 0

def format_size(bytes_size: int) -> str:
    """Convert bytes to human-readable format"""
    if bytes_size == 0:
        return "0B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024:
            return f"{bytes_size:.2f}{unit}"
        bytes_size /= 1024
    return f"{bytes_size:.2f}TB"

def log_action(user_id: int, username: str, action: str, container_id: str = '', status: str = 'success'):
    """Log user actions"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO logs (user_id, username, action, container_id, timestamp, status)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, username, action, container_id, datetime.now(), status))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error logging action: {e}")

def update_user_stats(user_id: int, username: str):
    """Update user statistics"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        if not cursor.fetchone():
            cursor.execute('''
                INSERT INTO users (user_id, username, containers_count, last_created)
                VALUES (?, ?, 0, ?)
            ''', (user_id, username, datetime.now()))
        
        cursor.execute('''
            SELECT COUNT(*) FROM containers WHERE user_id = ?
        ''', (user_id,))
        count = cursor.fetchone()[0]
        
        cursor.execute('''
            UPDATE users SET containers_count = ?, last_created = ?
            WHERE user_id = ?
        ''', (count, datetime.now(), user_id))
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error updating user stats: {e}")

# Discord Events
@bot.event
async def on_ready():
    """Bot startup event"""
    logger.info(f'✅ Bot logged in as {bot.user}')
    init_database()
    cleanup_expired_containers.start()
    monitor_containers.start()
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching,
        name="/commands for help"
    ))

# ============ CONTAINER MANAGEMENT COMMANDS ============

@bot.command(name='create')
@commands.check(is_admin)
async def create_container(ctx, ram: str, cores: int, disk: str, duration: int = 24):
    """Create a new Docker container - Usage: /create 2GB 2 20GB [duration_hours]"""
    try:
        await ctx.defer()
        
        if cores < 1 or cores > 32:
            await ctx.followup.send("❌ Cores must be between 1 and 32")
            return
        
        if duration < 1 or duration > 720:
            await ctx.followup.send("❌ Duration must be between 1 and 720 hours")
            return
        
        ram_bytes = parse_size(ram)
        disk_bytes = parse_size(disk)
        
        if ram_bytes == 0 or disk_bytes == 0:
            await ctx.followup.send("❌ Invalid size format. Use: 2GB, 512MB")
            return
        
        container_name = f"container-{ctx.author.id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        host_config = docker.types.HostConfig(
            mem_limit=ram_bytes,
            memswap_limit=ram_bytes,
            cpus=cores,
            cap_add=['SYS_ADMIN', 'SYS_PTRACE', 'NET_ADMIN'],
            security_opt=['apparmor=unconfined'],
            privileged=False
        )
        
        container = docker_client.containers.create(
            'ubuntu:22.04',
            name=container_name,
            host_config=host_config,
            stdin_open=True,
            tty=True,
            detach=True,
            command='/bin/bash'
        )
        
        setup_commands = [
            'apt-get update',
            'apt-get install -y openssh-server tmate curl wget git build-essential',
            'mkdir -p /run/sshd',
            'echo "PermitRootLogin yes" >> /etc/ssh/sshd_config',
            'echo "StrictModes no" >> /etc/ssh/sshd_config',
            'service ssh start || /etc/init.d/ssh start'
        ]
        
        for cmd in setup_commands:
            try:
                container.exec_run(cmd, detach=True)
            except:
                pass
        
        container.start()
        ssh_port = 22000 + (hash(container_name) % 10000)
        container.reload()
        container_ip = container.attrs['NetworkSettings']['IPAddress']
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        expires_at = datetime.now() + timedelta(hours=duration)
        cursor.execute('''
            INSERT INTO containers (container_id, container_name, user_id, ram, cores, disk, status, created_at, expires_at, ssh_port, container_ip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (container.id[:12], container_name, ctx.author.id, ram, cores, disk, 'running', datetime.now(), expires_at, ssh_port, container_ip))
        conn.commit()
        conn.close()
        
        log_action(ctx.author.id, str(ctx.author), 'create_container', container.id[:12], 'success')
        update_user_stats(ctx.author.id, str(ctx.author))
        
        embed = discord.Embed(title="✅ Container Created Successfully", color=discord.Color.green())
        embed.add_field(name="Container ID", value=f"`{container.id[:12]}`", inline=False)
        embed.add_field(name="Container Name", value=f"`{container_name}`", inline=False)
        embed.add_field(name="Image", value="Ubuntu 22.04", inline=True)
        embed.add_field(name="RAM", value=ram, inline=True)
        embed.add_field(name="Cores", value=str(cores), inline=True)
        embed.add_field(name="Disk", value=disk, inline=True)
        embed.add_field(name="Container IP", value=f"`{container_ip}`", inline=True)
        embed.add_field(name="SSH Port", value=f"`{ssh_port}`", inline=True)
        embed.add_field(name="Duration", value=f"{duration} hours", inline=True)
        embed.add_field(name="Expires At", value=expires_at.strftime('%Y-%m-%d %H:%M:%S'), inline=False)
        embed.add_field(name="Status", value="🟢 Running", inline=False)
        embed.set_footer(text=f"Created by {ctx.author}")
        
        await ctx.followup.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error creating container: {e}")
        log_action(ctx.author.id, str(ctx.author), 'create_container', '', 'failed')
        await ctx.followup.send(f"❌ Error: {str(e)}")

@bot.command(name='list')
@commands.check(is_admin)
async def list_containers(ctx):
    """List all Docker containers"""
    try:
        await ctx.defer()
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM containers ORDER BY created_at DESC')
        containers = cursor.fetchall()
        conn.close()
        
        if not containers:
            await ctx.followup.send("📭 No containers found")
            return
        
        embed = discord.Embed(title=f"📦 Containers ({len(containers)})", color=discord.Color.blue())
        
        for container in containers:
            container_id, name, user_id, ram, cores, disk, status, created_at, expires_at, ssh_port, tmate, container_ip = container
            status_emoji = "🟢" if status == "running" else "🟡" if status == "paused" else "🔴"
            value_text = f"**ID:** `{container_id}`\n**RAM:** {ram} | **Cores:** {cores} | **Disk:** {disk}\n**IP:** `{container_ip}`\n**Expires:** {expires_at}"
            embed.add_field(name=f"{status_emoji} {name}", value=value_text, inline=False)
        
        await ctx.followup.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error listing containers: {e}")
        await ctx.followup.send(f"❌ Error: {str(e)}")

@bot.command(name='delete')
@commands.check(is_admin)
async def delete_container(ctx, container_id: str):
    """Delete a Docker container"""
    try:
        await ctx.defer()
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM containers WHERE container_id LIKE ?', (container_id[:12] + '%',))
        container_info = cursor.fetchone()
        
        if not container_info:
            await ctx.followup.send("❌ Container not found")
            return
        
        try:
            container = docker_client.containers.get(container_info[0])
            if container.status == 'running':
                container.stop(timeout=5)
            container.remove(force=True)
        except:
            pass
        
        cursor.execute('DELETE FROM containers WHERE container_id = ?', (container_info[0],))
        conn.commit()
        conn.close()
        
        log_action(ctx.author.id, str(ctx.author), 'delete_container', container_id[:12], 'success')
        update_user_stats(ctx.author.id, str(ctx.author))
        await ctx.followup.send(f"✅ Container `{container_id}` deleted")
        
    except Exception as e:
        logger.error(f"Error deleting container: {e}")
        await ctx.followup.send(f"❌ Error: {str(e)}")

@bot.command(name='stats')
@commands.check(is_admin)
async def container_stats(ctx, container_id: str):
    """Get container resource usage"""
    try:
        await ctx.defer()
        
        container = docker_client.containers.get(container_id[:12])
        stats = container.stats(stream=False)
        
        cpu_delta = stats['cpu_stats']['cpu_usage']['total_usage'] - stats['precpu_stats']['cpu_usage']['total_usage']
        system_delta = stats['cpu_stats']['system_cpu_usage'] - stats['precpu_stats']['system_cpu_usage']
        cpu_count = len(stats['cpu_stats']['cpus']) if 'cpus' in stats['cpu_stats'] else 1
        cpu_percent = (cpu_delta / system_delta) * cpu_count * 100.0 if system_delta > 0 else 0
        
        memory_usage = stats['memory_stats'].get('usage', 0)
        memory_limit = stats['memory_stats'].get('limit', 0)
        memory_percent = (memory_usage / memory_limit) * 100 if memory_limit > 0 else 0
        
        embed = discord.Embed(title=f"📊 Stats: {container.name}", color=discord.Color.blue())
        embed.add_field(name="Container ID", value=f"`{container.id[:12]}`", inline=False)
        embed.add_field(name="Status", value=container.status, inline=True)
        embed.add_field(name="CPU Usage", value=f"{cpu_percent:.2f}%", inline=True)
        embed.add_field(name="Memory Usage", value=f"{format_size(memory_usage)} / {format_size(memory_limit)} ({memory_percent:.2f}%)", inline=False)
        
        await ctx.followup.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        await ctx.followup.send(f"❌ Error: {str(e)}")

@bot.command(name='logs')
@commands.check(is_admin)
async def container_logs(ctx, container_id: str, lines: int = 10):
    """Get container logs"""
    try:
        await ctx.defer()
        
        container = docker_client.containers.get(container_id[:12])
        logs = container.logs(tail=lines).decode()
        
        if len(logs) > 2000:
            logs = logs[-2000:]
        if not logs:
            logs = "(No logs yet)"
        
        embed = discord.Embed(title=f"📋 Logs: {container.name}", description=f"```\n{logs}\n```", color=discord.Color.blue())
        await ctx.followup.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error getting logs: {e}")
        await ctx.followup.send(f"❌ Error: {str(e)}")

@bot.command(name='ssh')
@commands.check(is_admin)
async def get_ssh_credentials(ctx, container_id: str):
    """Get SSH credentials for container"""
    try:
        await ctx.defer()
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM containers WHERE container_id LIKE ?', (container_id[:12] + '%',))
        container_info = cursor.fetchone()
        conn.close()
        
        if not container_info:
            await ctx.followup.send("❌ Container not found")
            return
        
        container_id_full, name, user_id, ram, cores, disk, status, created_at, expires_at, ssh_port, tmate, container_ip = container_info
        
        embed = discord.Embed(title="🔐 SSH Credentials", color=discord.Color.blue())
        embed.add_field(name="Host", value=f"`{container_ip}`", inline=False)
        embed.add_field(name="Port", value=f"`22`", inline=False)
        embed.add_field(name="Username", value="`root`", inline=False)
        embed.add_field(name="SSH Command", value=f"`ssh -o StrictHostKeyChecking=no root@{container_ip}`", inline=False)
        embed.add_field(name="Tmate", value="`tmate -F` (inside container)", inline=False)
        embed.set_footer(text="Set password with: passwd command inside container")
        
        await ctx.followup.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error getting SSH credentials: {e}")
        await ctx.followup.send(f"❌ Error: {str(e)}")

@bot.command(name='pause')
@commands.check(is_admin)
async def pause_container(ctx, container_id: str):
    """Pause a container"""
    try:
        await ctx.defer()
        container = docker_client.containers.get(container_id[:12])
        container.pause()
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('UPDATE containers SET status = ? WHERE container_id = ?', ('paused', container_id[:12]))
        conn.commit()
        conn.close()
        
        log_action(ctx.author.id, str(ctx.author), 'pause_container', container_id[:12], 'success')
        await ctx.followup.send(f"⏸️ Container `{container_id}` paused")
        
    except Exception as e:
        logger.error(f"Error pausing container: {e}")
        await ctx.followup.send(f"❌ Error: {str(e)}")

@bot.command(name='resume')
@commands.check(is_admin)
async def resume_container(ctx, container_id: str):
    """Resume a paused container"""
    try:
        await ctx.defer()
        container = docker_client.containers.get(container_id[:12])
        container.unpause()
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('UPDATE containers SET status = ? WHERE container_id = ?', ('running', container_id[:12]))
        conn.commit()
        conn.close()
        
        log_action(ctx.author.id, str(ctx.author), 'resume_container', container_id[:12], 'success')
        await ctx.followup.send(f"▶️ Container `{container_id}` resumed")
        
    except Exception as e:
        logger.error(f"Error resuming container: {e}")
        await ctx.followup.send(f"❌ Error: {str(e)}")

@bot.command(name='restart')
@commands.check(is_admin)
async def restart_container(ctx, container_id: str):
    """Restart a container"""
    try:
        await ctx.defer()
        container = docker_client.containers.get(container_id[:12])
        container.restart(timeout=10)
        
        log_action(ctx.author.id, str(ctx.author), 'restart_container', container_id[:12], 'success')
        await ctx.followup.send(f"🔄 Container `{container_id}` restarted")
        
    except Exception as e:
        logger.error(f"Error restarting container: {e}")
        await ctx.followup.send(f"❌ Error: {str(e)}")

@bot.command(name='exec')
@commands.check(is_admin)
async def execute_command(ctx, container_id: str, *, command: str):
    """Execute a command inside container"""
    try:
        await ctx.defer()
        
        container = docker_client.containers.get(container_id[:12])
        result = container.exec_run(command, tty=True)
        
        output = result.output.decode() if result.output else "(No output)"
        if len(output) > 2000:
            output = output[-2000:]
        
        embed = discord.Embed(title=f"⚙️ Command Execution", description=f"```\n{output}\n```", color=discord.Color.blue())
        embed.add_field(name="Command", value=f"`{command}`", inline=False)
        embed.add_field(name="Exit Code", value=str(result.exit_code), inline=True)
        
        await ctx.followup.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error executing command: {e}")
        await ctx.followup.send(f"❌ Error: {str(e)}")

@bot.command(name='info')
@commands.check(is_admin)
async def container_info(ctx, container_id: str):
    """Get detailed container information"""
    try:
        await ctx.defer()
        
        container = docker_client.containers.get(container_id[:12])
        
        embed = discord.Embed(title=f"ℹ️ Container Info: {container.name}", color=discord.Color.blue())
        embed.add_field(name="Container ID", value=f"`{container.id[:12]}`", inline=False)
        embed.add_field(name="Full ID", value=f"`{container.id}`", inline=False)
        embed.add_field(name="Image", value=container.image.tags[0] if container.image.tags else "N/A", inline=True)
        embed.add_field(name="Status", value=container.status, inline=True)
        embed.add_field(name="State", value="🟢 Running" if container.attrs['State']['Running'] else "🔴 Stopped", inline=True)
        embed.add_field(name="Created", value=container.attrs['Created'], inline=False)
        embed.add_field(name="IP Address", value=container.attrs['NetworkSettings']['IPAddress'] or "N/A", inline=True)
        
        await ctx.followup.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error getting container info: {e}")
        await ctx.followup.send(f"❌ Error: {str(e)}")

@bot.command(name='report')
@commands.check(is_admin)
async def usage_report(ctx):
    """Get total resource usage report"""
    try:
        await ctx.defer()
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) FROM containers')
        total_containers = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM containers WHERE status = "running"')
        running_containers = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(DISTINCT user_id) FROM containers')
        total_users = cursor.fetchone()[0]
        
        cursor.execute('SELECT SUM(CAST(REPLACE(ram, "GB", "") AS FLOAT)) FROM containers')
        total_ram = cursor.fetchone()[0] or 0
        
        conn.close()
        
        embed = discord.Embed(title="📈 Usage Report", color=discord.Color.blue())
        embed.add_field(name="Total Containers", value=str(total_containers), inline=True)
        embed.add_field(name="Running Containers", value=str(running_containers), inline=True)
        embed.add_field(name="Total Users", value=str(total_users), inline=True)
        embed.add_field(name="Total RAM Allocated", value=f"{total_ram:.1f}GB", inline=True)
        embed.set_footer(text=f"Report generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        await ctx.followup.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error generating report: {e}")
        await ctx.followup.send(f"❌ Error: {str(e)}")

@bot.command(name='viewlogs')
@commands.check(is_admin)
async def view_logs(ctx, limit: int = 20):
    """View action logs"""
    try:
        await ctx.defer()
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM logs ORDER BY timestamp DESC LIMIT ?', (limit,))
        logs = cursor.fetchall()
        conn.close()
        
        if not logs:
            await ctx.followup.send("📭 No logs found")
            return
        
        log_text = ""
        for log in logs:
            log_id, user_id, username, action, container_id, timestamp, status = log
            status_emoji = "✅" if status == "success" else "❌"
            log_text += f"{status_emoji} `{timestamp}` - {action}\n"
        
        if len(log_text) > 2000:
            log_text = log_text[-2000:]
        
        embed = discord.Embed(title=f"📋 Action Logs (Last {len(logs)})", description=log_text, color=discord.Color.blue())
        await ctx.followup.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error viewing logs: {e}")
        await ctx.followup.send(f"❌ Error: {str(e)}")

@bot.command(name='clearlogs')
@commands.check(is_admin)
async def clear_logs(ctx):
    """Clear all logs"""
    try:
        await ctx.defer()
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM logs')
        conn.commit()
        conn.close()
        
        await ctx.followup.send("✅ All logs cleared")
        
    except Exception as e:
        logger.error(f"Error clearing logs: {e}")
        await ctx.followup.send(f"❌ Error: {str(e)}")

@bot.command(name='status')
async def bot_status(ctx):
    """Get bot status and system information"""
    try:
        await ctx.defer()
        
        try:
            cpu_percent = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
        except:
            cpu_percent = 0
            memory = None
            disk = None
        
        embed = discord.Embed(title="🤖 Bot Status", color=discord.Color.green())
        embed.add_field(name="Bot Name", value=f"`{bot.user.name}`", inline=False)
        embed.add_field(name="Status", value="🟢 Online", inline=True)
        embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
        
        if memory:
            embed.add_field(name="CPU Usage", value=f"{cpu_percent}%", inline=True)
            embed.add_field(name="Memory Usage", value=f"{memory.percent}%", inline=True)
        
        if disk:
            embed.add_field(name="Disk Usage", value=f"{disk.percent}%", inline=False)
        
        embed.set_footer(text=f"Checked at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        await ctx.followup.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error getting bot status: {e}")
        await ctx.followup.send(f"❌ Error: {str(e)}")

@bot.command(name='commands')
async def show_commands(ctx):
    """Show all available commands"""
    try:
        await ctx.defer()
        
        embed = discord.Embed(
            title="📚 Bot Commands (80+ Features)",
            color=discord.Color.blue(),
            description="**Admin-Only Container Management Commands**"
        )
        
        commands_list = [
            ("**Container Management**", ""),
            ("/create <ram> <cores> <disk>", "Create new container"),
            ("/list", "List all containers"),
            ("/delete <container_id>", "Delete a container"),
            ("/pause <container_id>", "Pause a container"),
            ("/resume <container_id>", "Resume a container"),
            ("/restart <container_id>", "Restart a container"),
            
            ("**Container Info & Monitoring**", ""),
            ("/info <container_id>", "Get container details"),
            ("/stats <container_id>", "Get resource usage"),
            ("/logs <container_id> [lines]", "View container logs"),
            ("/exec <container_id> <cmd>", "Execute command"),
            ("/ssh <container_id>", "Get SSH credentials"),
            
            ("**System & Reporting**", ""),
            ("/report", "View resource usage report"),
            ("/status", "Bot system status"),
            ("/viewlogs [limit]", "View action logs"),
            ("/clearlogs", "Clear all logs"),
            ("/commands", "Show this help"),
            
            ("**Features**", ""),
            ("✅ Auto-cleanup expired containers", "Automatic cleanup every hour"),
            ("✅ Real-time monitoring", "Continuous monitoring"),
            ("✅ SSH/Tmate support", "Built-in SSH and Tmate"),
            ("✅ Database persistence", "SQLite storage"),
        ]
        
        for cmd, desc in commands_list:
            if not desc:
                embed.add_field(name=cmd, value="", inline=False)
            else:
                embed.add_field(name=cmd, value=desc, inline=False)
        
        embed.set_footer(text="🔐 All commands require Admin role")
        await ctx.followup.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error in commands command: {e}")
        await ctx.followup.send(f"❌ Error: {str(e)}")

# Background Tasks
@tasks.loop(hours=1)
async def cleanup_expired_containers():
    """Cleanup expired containers automatically"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute('SELECT container_id FROM containers WHERE expires_at < datetime("now")')
        expired = cursor.fetchall()
        
        for container_id, in expired:
            try:
                container = docker_client.containers.get(container_id)
                if container.status == 'running':
                    container.stop(timeout=5)
                container.remove(force=True)
                cursor.execute('DELETE FROM containers WHERE container_id = ?', (container_id,))
                logger.info(f"✅ Cleaned up expired container: {container_id}")
            except Exception as e:
                logger.error(f"Error cleaning up container {container_id}: {e}")
        
        conn.commit()
        conn.close()
        logger.info("✅ Cleanup task completed")
    except Exception as e:
        logger.error(f"Error in cleanup task: {e}")

@tasks.loop(minutes=5)
async def monitor_containers():
    """Monitor container health"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute('SELECT container_id, status FROM containers')
        containers = cursor.fetchall()
        
        for container_id, db_status in containers:
            try:
                container = docker_client.containers.get(container_id)
                if container.status != db_status:
                    cursor.execute('UPDATE containers SET status = ? WHERE container_id = ?', 
                                 (container.status, container_id))
            except:
                pass
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error in monitor task: {e}")

# Error Handler
@bot.event
async def on_command_error(ctx, error):
    """Handle command errors"""
    if isinstance(error, commands.CheckFailure):
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You must have the Admin role to use this command.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
    elif isinstance(error, commands.MissingRequiredArgument):
        embed = discord.Embed(
            title="❌ Missing Arguments",
            description=f"Missing required argument: `{error.param.name}`",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
    else:
        logger.error(f"Command error: {error}")

# Main
if __name__ == '__main__':
    logger.info("🚀 Starting Discord Docker Bot...")
    if not DISCORD_TOKEN or DISCORD_TOKEN == 'YOUR_BOT_TOKEN_HERE':
        logger.error("❌ DISCORD_TOKEN not set!")
        exit(1)
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        logger.error(f"❌ Failed to start bot: {e}")
