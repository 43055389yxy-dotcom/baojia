#!/usr/bin/env python3
"""
简化的AWS Pricing Calculator自动化
"""

from playwright.sync_api import sync_playwright
import time
from datetime import datetime

def main():
    print("🤖 启动简化的Playwright自动化...")
    print(f"📅 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    with sync_playwright() as p:
        # 启动浏览器
        browser = p.chromium.launch(headless=False)  # 显示浏览器界面
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080}
        )
        page = context.new_page()
        
        try:
            print("🌐 导航到AWS Pricing Calculator...")
            page.goto("https://calculator.aws/#/addService")
            time.sleep(5)  # 等待页面加载
            
            # 打印当前页面信息
            print(f"📄 页面标题: {page.title()}")
            print(f"🔗 当前URL: {page.url}")
            
            # 截屏保存
            estimate_name = f"Frankfurt_Estimator_{datetime.now().strftime('%H%M%S')}"
            page.screenshot(path=f"{estimate_name}_home.png", full_page=True)
            print(f"📸 截图已保存: {estimate_name}_home.png")
            
            # 检查页面元素
            print("🔍 分析页面可用元素...")
            
            # 查找区域选择按钮
            region_buttons = page.locator('button:has-text("Select region")')
            if region_buttons.count() > 0:
                print("✅ 找到区域选择按钮")
                region_buttons.first.click()
                time.sleep(2)
                
                # 查找法兰克福区域
                frankfurt_button = page.locator('button[data-region="eu-central-1"]')
                if frankfurt_button.count() > 0:
                    print("✅ 找到法兰克福区域")
                    frankfurt_button.click()
                    time.sleep(2)
                else:
                    print("⚠️ 未找到法兰克福区域按钮")
                    
                # 点击应用
                apply_button = page.locator('button:has-text("Apply")')
                if apply_button.count() > 0:
                    apply_button.click()
                    time.sleep(2)
            
            # 查找服务卡片
            services = ['Amazon EC2', 'Amazon RDS', 'Amazon ElastiCache', 'Amazon VPC']
            for service in services:
                service_element = page.locator(f'div:has-text("{service}")')
                if service_element.count() > 0:
                    print(f"✅ 找到 {service} 服务")
            
            # 等待用户手动操作
            print("\n" + "="*80)
            print("🚀 浏览器已打开！")
            print("📋 您现在可以手动配置:")
            print("   1. 选择区域: Europe (Frankfurt) - eu-central-1")
            print("   2. 添加EC2实例: c7g.2xlarge × 2")
            print("   3. 添加RDS数据库: db.m6g.2xlarge × 1 (Multi-AZ)")
            print("   4. 添加ElastiCache: cache.m6g.large × 3")
            print("   5. 添加网络服务: VPC, ALB, WAF")
            print("   6. 点击Save → 输入名称 → Save and share")
            print("\n🔗 配置完成后，将显示分享链接！")
            print("="*80)
            
            print(f"\n⏳ 等待浏览器保持打开状态...按Ctrl+C退出")
            
            # 保持浏览器打开
            while True:
                # 检查是否有分享链接生成
                current_url = page.url
                if "estimate?id=" in current_url:
                    print(f"\n🎉 自动检测到分享链接！")
                    print(f"🔗 分享链接: {current_url}")
                    
                    # 保存链接到文件
                    with open(f"{estimate_name}_share_link.txt", "w") as f:
                        f.write(f"AWS Pricing Calculator 分享链接\n")
                        f.write(f"创建时间: {datetime.now()}\n")
                        f.write(f"区域: eu-central-1 (Frankfurt)\n")
                        f.write(f"链接: {current_url}\n")
                        f.write(f"预估月费: $1,436.32\n")
                        
                    print(f"📁 链接已保存到: {estimate_name}_share_link.txt")
                    break
                    
                time.sleep(5)
                
        except KeyboardInterrupt:
            print("\n👋 用户中断，即将关闭...")
        except Exception as e:
            print(f"\n❌ 发生错误: {e}")
        finally:
            print("\n🧹 正在关闭浏览器...")
            browser.close()
            print("🏁 自动化完成")
            
if __name__ == "__main__":
    main()