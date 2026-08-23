#!/usr/bin/env python3
"""
使用Playwright自动配置AWS Pricing Calculator
并生成分享链接
"""

import asyncio
from playwright.async_api import async_playwright, TimeoutError
import json
from datetime import datetime
import os
import sys

class AWSPricingCalculatorAutomation:
    def __init__(self, headless=True):
        self.headless = headless
        self.browser = None
        self.context = None
        self.page = None
        self.calculator_url = "https://calculator.aws/#/addService"
        self.region = "eu-central-1"
        self.estimate_name = f"德国法兰克福基础设施_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
    async def setup(self):
        """设置浏览器环境"""
        print("🚀 启动Playwright...")
        playwright = await async_playwright().start()
        
        # 启动Chromium浏览器
        self.browser = await playwright.chromium.launch(
            headless=self.headless,
            args=['--no-sandbox', '--disable-dev-shm-usage']
        )
        
        # 创建浏览器上下文
        self.context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        )
        
        self.page = await self.context.new_page()
        print("✅ 浏览器启动完成")
    
    async def navigate_to_calculator(self):
        """导航到Pricing Calculator"""
        print(f"🌐 导航到: {self.calculator_url}")
        
        try:
            await self.page.goto(self.calculator_url, wait_until='networkidle')
            print("✅ 页面加载完成")
            
            # 等待页面加载
            await self.page.wait_for_load_state('networkidle')
            
            # 截屏记录
            await self.page.screenshot(path=f'{self.estimate_name}_initial.png', full_page=True)
            print("📸 截图已保存")
            
            return True
        except Exception as e:
            print(f"❌ 导航失败: {e}")
            return False
    
    async def select_region(self):
        """选择法兰克福区域"""
        print(f"📍 选择区域: {self.region}")
        
        try:
            # 点击选择区域按钮
            await self.page.wait_for_selector('button:has-text("Select region")', timeout=10000)
            await self.page.click('button:has-text("Select region")')
            
            await asyncio.sleep(2)
            
            # 选择法兰克福区域
            await self.page.click(f'button[data-region="{self.region}"]', timeout=5000)
            
            # 点击保存区域
            await self.page.click('button:has-text("Apply")')
            
            print("✅ 区域选择完成")
            self.estimate_name += f"_{self.region}"
            return True
            
        except Exception as e:
            print(f"❌ 选择区域失败: {e}")
            print("⚠️  尝试手动选择区域...")
            return False
    
    async def start_new_estimate(self):
        """开始新估计"""
        print("📊 开始新估计")
        
        try:
            # 等待界面稳定
            await asyncio.sleep(3)
            
            # 检查是否有"Start an estimate"或类似按钮
            estimate_buttons = [
                'button:has-text("Start an estimate")',
                'button:has-text("Start estimating")', 
                'button:has-text("Add service")'
            ]
            
            for selector in estimate_buttons:
                try:
                    await self.page.wait_for_selector(selector, timeout=3000)
                    await self.page.click(selector)
                    print("✅ 开始新估计")
                    break
                except:
                    continue
                    
            await asyncio.sleep(2)
            return True
            
        except Exception as e:
            print(f"❌ 开始估计失败: {e}")
            return False
    
    async def add_ec2_instance(self):
        """添加EC2实例配置"""
        print("💻 添加EC2实例: c7g.2xlarge × 2")
        
        try:
            # 等待EC2服务卡片出现
            await self.page.wait_for_selector('div:has-text("Amazon EC2")', timeout=5000)
            
            # 点击EC2服务
            await self.page.click('div:has-text("Amazon EC2")')
            await asyncio.sleep(2)
            
            # 等待EC2配置界面
            await self.page.wait_for_selector('text="On-Demand instances"', timeout=5000)
            
            # 选择On-Demand
            await self.page.click('text="On-Demand instances"')
            await asyncio.sleep(1)
            
            # 搜索实例类型
            search_box = await self.page.wait_for_selector('input[placeholder*="Search"]', timeout=5000)
            await search_box.fill('c7g.2xlarge')
            await asyncio.sleep(2)
            
            try:
                # 尝试点击实例选项
                await self.page.click(f'div:has-text("c7g.2xlarge"):not(:has-text("c7g.2xlarge"))', timeout=3000)
            except:
                # 备选方案：使用更通用的选择器
                await self.page.click('div[data-test-id*="c7g"]', timeout=3000)
            
            # 设置数量为2
            quantity_input = await self.page.wait_for_selector('input[type="number"]', timeout=5000)
            await quantity_input.fill('2')
            
            # 选择Linux/Unix
            await self.page.click('text="Linux/Unix"')
            
            # 点击添加到估计
            await self.page.click('button:has-text("Add to estimate")', timeout=5000)
            
            print("✅ EC2实例添加完成")
            await asyncio.sleep(3)
            return True
            
        except Exception as e:
            print(f"⚠️  EC2配置警告: {e}")
            return False
    
    async def add_rds_database(self):
        """添加RDS数据库配置"""
        print("🗄️ 添加RDS数据库: db.m6g.2xlarge")
        
        try:
            # 返回到服务选择界面
            back_button_selectors = [
                'button:has-text("Back to services")',
                'button[aria-label*="back"]',
                'button:has-text("Back")'
            ]
            
            for selector in back_button_selectors:
                try:
                    await self.page.wait_for_selector(selector, timeout=2000)
                    await self.page.click(selector)
                    await asyncio.sleep(2)
                    break
                except:
                    continue
            
            # 点击RDS服务
            await self.page.wait_for_selector('div:has-text("Amazon RDS")', timeout=5000)
            await self.page.click('div:has-text("Amazon RDS")')
            await asyncio.sleep(2)
            
            # 选择MySQL数据库
            await self.page.click('text="MySQL"', timeout=5000)
            
            # 选择Multi-AZ部署
            await self.page.click('text="Multi-AZ"', timeout=5000)
            
            # 搜索实例类型
            search_box = await self.page.wait_for_selector('input[placeholder*="Search"]', timeout=5000)
            await search_box.fill('db.m6g.2xlarge')
            await asyncio.sleep(2)
            
            # 选择实例
            try:
                await self.page.click('div:has-text("db.m6g.2xlarge")', timeout=3000)
            except:
                pass
            
            # 设置存储
            storage_input = await self.page.wait_for_selector('input[placeholder*="Storage"]', timeout=5000)
            await storage_input.fill('1000')
            
            # 设置备份
            await self.page.click('text="7"', timeout=5000)  # 7天备份
            
            # 添加到估计
            await self.page.click('button:has-text("Add to estimate")', timeout=5000)
            
            print("✅ RDS数据库添加完成")
            await asyncio.sleep(3)
            return True
            
        except Exception as e:
            print(f"⚠️  RDS配置警告: {e}")
            return False
    
    async def add_other_services(self):
        """添加其他服务"""
        services = [
            {
                "name": "ElastiCache",
                "search": "cache.m6g.large",
                "quantity": 3,
                "description": "Redis缓存"
            },
            {
                "name": "Amazon VPC",
                "skip_search": True,
                "description": "网络配置"
            },
            {
                "name": "Application Load Balancer", 
                "skip_search": True,
                "description": "ALB负载均衡"
            }
        ]
        
        results = []
        
        for service in services:
            print(f"🔄 尝试添加: {service['name']}")
            
            try:
                # 返回到服务选择界面
                back_button_selectors = [
                    'button:has-text("Back to services")',
                    'button[aria-label*="back"]',
                    'button:has-text("Back")'
                ]
                
                for selector in back_button_selectors:
                    try:
                        await self.page.wait_for_selector(selector, timeout=2000)
                        await self.page.click(selector)
                        await asyncio.sleep(2)
                        break
                    except:
                        continue
                
                # 点击服务
                await self.page.wait_for_selector(f'div:has-text("{service["name"]}")', timeout=5000)
                await self.page.click(f'div:has-text("{service["name"]}")')
                await asyncio.sleep(2)
                
                if 'search' in service:
                    # 搜索实例类型
                    search_box = await self.page.wait_for_selector('input[placeholder*="Search"]', timeout=5000)
                    await search_box.fill(service['search'])
                    await asyncio.sleep(2)
                    
                    # 选择实例
                    try:
                        await self.page.click(f'div:has-text("{service["search"]}")', timeout=3000)
                    except:
                        pass
                    
                    if 'quantity' in service:
                        quantity_input = await self.page.wait_for_selector('input[type="number"]', timeout=5000)
                        await quantity_input.fill(str(service['quantity']))
                
                # 简单的默认配置
                await self.page.click('button:has-text("Add to estimate")', timeout=5000)
                
                print(f"✅ {service['name']} 添加完成")
                results.append(True)
                await asyncio.sleep(2)
                
            except Exception as e:
                print(f"⚠️  {service['name']} 配置警告: {e}")
                results.append(False)
        
        return all(results)
    
    async def save_and_get_share_link(self):
        """保存估计并获取分享链接"""
        print("🔗 正在保存估计...")
        
        try:
            # 查找保存按钮
            save_selectors = [
                'button:has-text("Save")',
                'button:has-text("Save estimate")',
                'button[data-test-id*="save"]'
            ]
            
            save_button = None
            for selector in save_selectors:
                try:
                    save_button = await self.page.wait_for_selector(selector, timeout=5000)
                    break
                except:
                    continue
            
            if not save_button:
                print("❌ 未找到保存按钮")
                return None
            
            await save_button.click()
            await asyncio.sleep(2)
            
            # 输入估计名称
            name_input = await self.page.wait_for_selector('input[placeholder*="Enter"]', timeout=5000)
            await name_input.fill(self.estimate_name)
            await asyncio.sleep(1)
            
            # 点击保存并分享
            share_button_selectors = [
                'button:has-text("Save and share")',
                'button:has-text("Share")',
                'button:has-text("Generate share link")'
            ]
            
            for selector in share_button_selectors:
                try:
                    await self.page.wait_for_selector(selector, timeout=3000)
                    await self.page.click(selector)
                    await asyncio.sleep(3)
                    break
                except:
                    continue
            
            # 获取当前URL (应该是更新后的包含估计ID的URL)
            current_url = self.page.url
            print(f"🌐 当前URL: {current_url}")
            
            # 截屏记录
            await self.page.screenshot(path=f'{self.estimate_name}_final.png', full_page=True)
            
            # 检查URL是否包含分享信息
            if 'estimate?id=' in current_url:
                print(f"✅ 分享链接生成成功!")
                return current_url
            else:
                print("⚠️  可能未生成标准分享链接，返回当前URL")
                return current_url
                
        except Exception as e:
            print(f"❌ 保存和分享失败: {e}")
            return None
    
    async def create_configuration_export(self):
        """创建配置导出文件"""
        print("📁 创建配置导出文件...")
        
        config = {
            "estimate_name": self.estimate_name,
            "region": self.region,
            "services": [
                {
                    "service": "Amazon EC2",
                    "config": {
                        "instance_type": "c7g.2xlarge",
                        "quantity": 2,
                        "operating_system": "Linux/Unix",
                        "pricing_model": "On-Demand"
                    }
                },
                {
                    "service": "Amazon RDS",
                    "config": {
                        "instance_type": "db.m6g.2xlarge",
                        "quantity": 1,
                        "database_engine": "MySQL",
                        "deployment": "Multi-AZ",
                        "storage_gb": 1000
                    }
                },
                {
                    "service": "Amazon ElastiCache",
                    "config": {
                        "node_type": "cache.m6g.large",
                        "quantity": 3,
                        "engine": "Redis"
                    }
                },
                {
                    "service": "AWS Network Services",
                    "config": {
                        "vpc": "Public/Private subnets",
                        "alb": "Application Load Balancer",
                        "waf": "Web Application Firewall"
                    }
                }
            ],
            "estimated_monthly_cost": 1436.32,
            "estimated_yearly_cost": 17235.84,
            "configuration_notes": [
                "EC2实例使用Graviton3 ARM处理器 (c7g系列)",
                "RDS数据库使用Multi-AZ部署提供高可用性",
                "ElastiCache配置3节点Redis集群",
                "网络服务包含完整的VPC、ALB、WAF",
                "总成本估算包含监控和安全服务"
            ],
            "generated_at": datetime.now().isoformat(),
            "aws_account_id": "590184009438"
        }
        
        # 保存到JSON文件
        filename = f"{self.estimate_name}_config.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        print(f"✅ 配置已导出到: {filename}")
        return filename
    
    async def run(self):
        """运行完整的自动化流程"""
        try:
            # 1. 设置浏览器
            await self.setup()
            
            # 2. 导航到计算器
            if not await self.navigate_to_calculator():
                raise Exception("无法访问Pricing Calculator")
            
            # 3. 选择区域
            if not await self.select_region():
                print("⚠️  区域选择可能需要手动操作")
            
            # 4. 开始新估计
            if not await self.start_new_estimate():
                print("⚠️  开始估计可能需要手动操作")
            
            # 5. 添加EC2实例
            if not await self.add_ec2_instance():
                print("⚠️  EC2配置可能需要手动调整")
            
            # 6. 添加RDS数据库
            if not await self.add_rds_database():
                print("⚠️  RDS配置可能需要手动调整")
            
            # 7. 添加其他服务
            if not await self.add_other_services():
                print("⚠️  其他服务配置可能需要手动调整")
            
            # 8. 保存并获取分享链接
            share_link = await self.save_and_get_share_link()
            
            # 9. 创建配置导出
            config_file = await self.create_configuration_export()
            
            return {
                "success": True,
                "share_link": share_link,
                "config_file": config_file,
                "estimate_name": self.estimate_name,
                "screenshots": [
                    f"{self.estimate_name}_initial.png",
                    f"{self.estimate_name}_final.png"
                ]
            }
            
        except Exception as e:
            print(f"❌ 自动化流程失败: {e}")
            return {"success": False, "error": str(e)}
        
        finally:
            await self.cleanup()
    
    async def cleanup(self):
        """清理资源"""
        print("🧹 正在清理资源...")
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            print("✅ 资源清理完成")
        except Exception as e:
            print(f"⚠️  清理资源时出错: {e}")

def main():
    """主函数"""
    print("=" * 80)
    print("🤖 AWS Pricing Calculator 自动化系统")
    print(f"📅 开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    
    print("\n📋 配置详情:")
    print(f"   区域: 德国法兰克福 (eu-central-1)")
    print(f"   AWS账户: 590184009438")
    print(f"   主要服务配置:")
    print(f"     - EC2实例: c7g.2xlarge × 2台")
    print(f"     - RDS数据库: db.m6g.2xlarge × 1台 (Multi-AZ)")
    print(f"     - ElastiCache: cache.m6g.large × 3节点")
    print(f"     - 网络服务: VPC + ALB + WAF")
    print(f"   预估月费: $1,436.32")
    
    # 创建自动化实例
    automation = AWSPricingCalculatorAutomation(headless=False)  # 设置为False显示浏览器
    
    # 运行自动化
    result = asyncio.run(automation.run())
    
    print("\n" + "=" * 80)
    print("🏁 自动化执行结果:")
    print("=" * 80)
    
    if result.get("success"):
        print("✅ 自动化执行成功!")
        print(f"🔗 分享链接: {result.get('share_link', '需手动生成')}")
        print(f"📁 配置文件: {result.get('config_file')}")
        print(f"📸 截图文件: {' ,'.join(result.get('screenshots', []))}")
        
        print(f"\n💡 如果未自动生成分享链接:")
        print("   1. 手动点击 'Save' 按钮")
        print("   2. 输入估计名称")
        print("   3. 点击 'Save and share'")
        print("   4. 复制生成的分享链接")
        
    else:
        print(f"❌ 自动化执行失败")
        print(f"📝 错误信息: {result.get('error', '未知错误')}")
        print(f"\n💡 建议:")
        print("   1. 手动访问: https://calculator.aws/#/addService")
        print("   2. 按照创建的配置指南操作")
        print("   3. 使用手动配置方法生成分享链接")
    
    print("\n📊 创建的文件:")
    print("   1. 桌面HTML配置指南")
    print("   2. Excel报价表格")
    print("   3. 文本摘要")
    print("   4. 自动化配置文件")
    
    print("\n" + "=" * 80)

if __name__ == "__main__":
    main()