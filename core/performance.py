import time
import asyncio
import psutil
import os
from typing import Dict, List
from functools import wraps
from core.logger import logger


class PerformanceMonitor:
    """应用性能监控器"""
    
    def __init__(self):
        self.metrics = {
            "requests": 0,
            "total_time": 0.0,
            "avg_response_time": 0.0,
            "max_response_time": 0.0,
            "min_response_time": float('inf'),
            "errors": 0,
        }
        self.recent_requests = []
        self.max_recent_requests = 100
    
    def record_request(self, response_time: float, is_error: bool = False):
        """记录请求性能指标"""
        self.metrics["requests"] += 1
        self.metrics["total_time"] += response_time
        self.metrics["avg_response_time"] = self.metrics["total_time"] / self.metrics["requests"]
        
        if response_time > self.metrics["max_response_time"]:
            self.metrics["max_response_time"] = response_time
        
        if response_time < self.metrics["min_response_time"]:
            self.metrics["min_response_time"] = response_time
        
        if is_error:
            self.metrics["errors"] += 1
        
        # 保持最近的请求记录
        self.recent_requests.append({
            "timestamp": time.time(),
            "response_time": response_time,
            "is_error": is_error
        })
        
        if len(self.recent_requests) > self.max_recent_requests:
            self.recent_requests.pop(0)
    
    def get_system_metrics(self) -> Dict:
        """获取系统性能指标"""
        try:
            cpu_percent = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            
            return {
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent,
                "memory_available_mb": memory.available / (1024 * 1024),
                "disk_percent": disk.percent,
                "disk_free_gb": disk.free / (1024 * 1024 * 1024),
            }
        except Exception as e:
            logger.warning(f"获取系统指标失败: {str(e)}")
            return {}
    
    def get_performance_report(self) -> Dict:
        """获取性能报告"""
        system_metrics = self.get_system_metrics()
        
        # 计算最近1分钟的请求数
        current_time = time.time()
        recent_minute_requests = len([
            req for req in self.recent_requests 
            if current_time - req["timestamp"] <= 60
        ])
        
        return {
            "requests": self.metrics,
            "system": system_metrics,
            "requests_per_minute": recent_minute_requests,
            "error_rate": (self.metrics["errors"] / max(1, self.metrics["requests"])) * 100
        }


# 全局性能监控器实例
performance_monitor = PerformanceMonitor()


def monitor_performance(func):
    """性能监控装饰器"""
    @wraps(func)
    async def async_wrapper(*args, **kwargs):
        start_time = time.time()
        is_error = False
        
        try:
            result = await func(*args, **kwargs)
            return result
        except Exception as e:
            is_error = True
            raise
        finally:
            end_time = time.time()
            response_time = end_time - start_time
            performance_monitor.record_request(response_time, is_error)
    
    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        start_time = time.time()
        is_error = False
        
        try:
            result = func(*args, **kwargs)
            return result
        except Exception as e:
            is_error = True
            raise
        finally:
            end_time = time.time()
            response_time = end_time - start_time
            performance_monitor.record_request(response_time, is_error)
    
    return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper


async def performance_cleanup_task():
    """定期清理性能数据的后台任务"""
    while True:
        try:
            await asyncio.sleep(300)  # 每5分钟执行一次
            
            # 清理过期的请求记录
            current_time = time.time()
            cutoff_time = current_time - 3600  # 保留最近1小时的数据
            
            performance_monitor.recent_requests = [
                req for req in performance_monitor.recent_requests
                if req["timestamp"] > cutoff_time
            ]
            
            # 记录性能报告
            report = performance_monitor.get_performance_report()
            if report["system"]:
                logger.info(f"性能指标 - CPU: {report['system']['cpu_percent']:.1f}%, "
                          f"内存: {report['system']['memory_percent']:.1f}%, "
                          f"每分钟请求数: {report['requests_per_minute']}, "
                          f"平均响应时间: {report['requests']['avg_response_time']:.3f}s")
                
                # 性能警告
                if report["system"]["cpu_percent"] > 80:
                    logger.warning("CPU使用率过高!")
                if report["system"]["memory_percent"] > 85:
                    logger.warning("内存使用率过高!")
                if report["requests"]["avg_response_time"] > 2.0:
                    logger.warning("平均响应时间过长!")
                    
        except Exception as e:
            logger.error(f"性能监控任务错误: {str(e)}")


def get_performance_recommendations() -> List[str]:
    """获取性能优化建议"""
    recommendations = []
    report = performance_monitor.get_performance_report()
    
    if report["requests"]["avg_response_time"] > 1.0:
        recommendations.append("平均响应时间较长，建议检查数据库查询和文件I/O操作")
    
    if report["error_rate"] > 5:
        recommendations.append(f"错误率较高({report['error_rate']:.1f}%)，建议检查错误日志")
    
    if report["system"].get("memory_percent", 0) > 80:
        recommendations.append("内存使用率高，建议增加内存或优化内存使用")
    
    if report["system"].get("cpu_percent", 0) > 70:
        recommendations.append("CPU使用率高，建议优化计算密集型操作")
    
    if report["requests_per_minute"] > 100:
        recommendations.append("请求量较大，建议考虑使用缓存和负载均衡")
    
    return recommendations


async def optimize_memory_usage():
    """内存使用优化"""
    try:
        import gc
        
        # 强制垃圾回收
        collected = gc.collect()
        
        # 获取内存使用情况
        process = psutil.Process(os.getpid())
        memory_info = process.memory_info()
        
        logger.info(f"内存优化完成 - 回收对象: {collected}, "
                   f"当前内存使用: {memory_info.rss / 1024 / 1024:.1f}MB")
        
        return {
            "collected_objects": collected,
            "memory_usage_mb": memory_info.rss / 1024 / 1024
        }
    except Exception as e:
        logger.error(f"内存优化失败: {str(e)}")
        return None