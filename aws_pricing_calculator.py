#!/usr/bin/env python3
"""
生成AWS Pricing Calculator配置和链接
"""

import json
from datetime import datetime

def create_calculator_config():
    """创建Pricing Calculator配置"""
    
    config = {
        "name": "德国法兰克福企业级基础设施",
        "region": "eu-central-1",
        "description": "根据客户需求配置的AWS法兰克福企业基础设施",
        "services": [
            {
                "service": "Amazon EC2",
                "description": "API网关写入服务实例",
                "configurations": [
                    {
                        "instance": "c7g.2xlarge",
                        "quantity": 2,
                        "operatingSystem": "Linux",
                        "tenancy": "Shared",
                        "pricing": "OnDemand",
                        "durationHours": 730,
                        "costPerMonth": 240.75
                    }
                ]
            },
            {
                "service": "Amazon RDS",
                "description": "MySQL主库 Multi-AZ",
                "configurations": [
                    {
                        "instance": "db.m6g.2xlarge",
                        "quantity": 1,
                        "databaseEngine": "MySQL",
                        "deploymentOption": "Multi-AZ",
                        "storageGB": 1000,
                        "pricing": "OnDemand",
                        "durationHours": 730,
                        "costPerMonth": 621.96
                    }
                ]
            },
            {
                "service": "Amazon ElastiCache",
                "description": "Redis缓存集群",
                "configurations": [
                    {
                        "instance": "cache.m6g.large",
                        "quantity": 3,
                        "cacheEngine": "Redis",
                        "pricing": "OnDemand",
                        "durationHours": 730,
                        "costPerMonth": 271.56
                    }
                ]
            },
            {
                "service": "AWS DMS",
                "description": "数据库迁移服务",
                "configurations": [
                    {
                        "instance": "dms.t3.large",
                        "quantity": 1,
                        "pricing": "OnDemand",
                        "durationHours": 730,
                        "costPerMonth": 113.15
                    }
                ]
            },
            {
                "service": "Amazon VPC",
                "description": "虚拟网络 + 子网",
                "configurations": [
                    {
                        "description": "VPC + 公网/私网子网",
                        "inclDefaultVPC": False,
                        "inclNATGateway": True,
                        "pricing": "Estimate",
                        "costPerMonth": 65.00
                    }
                ]
            },
            {
                "service": "Application Load Balancer",
                "description": "ALB负载均衡器",
                "configurations": [
                    {
                        "description": "公网监听器",
                        "capacityUnits": 22.5,
                        "pricing": "Estimate",
                        "costPerMonth": 22.50
                    }
                ]
            },
            {
                "service": "AWS WAF",
                "description": "Web应用防火墙",
                "configurations": [
                    {
                        "description": "WAF Web ACL",
                        "webACLUnits": 1,
                        "pricing": "Estimate",
                        "costPerMonth": 15.00
                    }
                ]
            },
            {
                "service": "Amazon S3",
                "description": "对象存储",
                "configurations": [
                    {
                        "storageClass": "Standard",
                        "storageGB": 500,
                        "requestsPerMonth": 1000000,
                        "pricing": "Estimate",
                        "costPerMonth": 25.00
                    }
                ]
            },
            {
                "service": "Amazon CloudWatch",
                "description": "监控服务",
                "configurations": [
                    {
                        "description": "指标+日志+仪表板",
                        "pricing": "Estimate",
                        "costPerMonth": 45.00
                    }
                ]
            },
            {
                "service": "AWS X-Ray",
                "description": "应用性能监控",
                "configurations": [
                    {
                        "description": "分布式追踪",
                        "tracesPerMonth": 1000000,
                        "pricing": "Estimate",
                        "costPerMonth": 15.00
                    }
                ]
            },
            {
                "service": "AWS Secrets Manager",
                "description": "密钥管理",
                "configurations": [
                    {
                        "description": "密钥存储",
                        "secretsCount": 10,
                        "pricing": "Estimate",
                        "costPerMonth": 0.40
                    }
                ]
            },
            {
                "service": "AWS KMS",
                "description": "密钥加密服务",
                "configurations": [
                    {
                        "description": "密钥加密",
                        "pricing": "Estimate",
                        "costPerMonth": 1.00
                    }
                ]
            }
        ]
    }
    
    return config

def generate_calculator_link(config):
    """生成Pricing Calculator预估链接"""
    
    total_cost = sum(
        service_config.get("costPerMonth", 0) 
        for service in config["services"] 
        for service_config in service["configurations"]
    )
    
    # 生成简化的共享链接格式
    link_data = {
        "estimateId": f"estimate-{datetime.now().strftime('%Y%m%d')}-frankfurt",
        "region": config["region"],
        "services": config["services"],
        "totalMonthlyCost": total_cost,
        "totalYearlyCost": total_cost * 12
    }
    
    # 创建可以导入的配置
    import_config = {
        "name": config["name"],
        "region": config["region"],
        "description": config["description"],
        "services": []
    }
    
    for service in config["services"]:
        simplified_service = {
            "service": service["service"],
            "quantity": sum([c.get("quantity", 1) for c in service["configurations"]]),
            "cost": sum([c.get("costPerMonth", 0) for c in service["configurations"]])
        }
        import_config["services"].append(simplified_service)
    
    return link_data, import_config

def main():
    """主函数"""
    print("=" * 80)
    print("🔧 AWS Pricing Calculator 配置生成器")
    print("=" * 80)
    
    # 创建配置
    config = create_calculator_config()
    
    # 生成链接和数据
    link_data, import_config = generate_calculator_link(config)
    
    # 打印配置信息
    print(f"\n📋 配置名称: {config['name']}")
    print(f"📍 区域: {config['region']} (德国法兰克福)")
    print(f"📅 创建时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 打印详细配置
    print("\n📊 服务配置详情:")
    print("-" * 80)
    
    for service in config["services"]:
        service_name = service["service"]
        service_desc = service["description"]
        
        configs_summary = []
        for cfg in service["configurations"]:
            if "instance" in cfg:
                configs_summary.append(f"{cfg['instance']} × {cfg.get('quantity', 1)}")
            else:
                configs_summary.append(service_desc)
        
        total_cost = sum([cfg.get("costPerMonth", 0) for cfg in service["configurations"]])
        
        print(f"🟢 {service_name:30} | {', '.join(configs_summary)[:40]:40} | ${total_cost:8.2f}/月")
    
    # 计算总成本
    total_monthly = link_data["totalMonthlyCost"]
    total_yearly = total_monthly * 12
    
    print("\n" + "=" * 80)
    print("💰 总成本估算")
    print("-" * 80)
    print(f"📈 按月支付: ${total_monthly:,.2f}")
    print(f"📈 按年支付: ${total_yearly:,.2f}")
    
    print("\n" + "=" * 80)
    print("🔗 Pricing Calculator 使用说明")
    print("=" * 80)
    
    # 提供手动配置指南
    instructions = [
        "1. 访问: https://calculator.aws/#/addService",
        "2. 选择区域: '德国法兰克福 (eu-central-1)'",
        "3. 按照以下步骤添加服务:",
        "",
        "📌 第1步: EC2实例",
        "   - 点击'Amazon EC2'",
        "   - 选择'On-Demand instances'",
        "   - 选择Linux/Unix",
        "   - 搜索并添加: c7g.2xlarge × 2",
        "",
        "📌 第2步: RDS数据库", 
        "   - 点击'Amazon RDS'",
        "   - 数据库类型: MySQL",
        "   - 部署选项: Multi-AZ",
        "   - 添加实例: db.m6g.2xlarge × 1",
        "   - 存储: 1000GB SSD",
        "",
        "📌 第3步: 网络服务",
        "   - VPC: 添加默认VPC + NAT网关",
        "   - ALB: 添加Application Load Balancer",
        "   - WAF: 添加Web Application Firewall",
        "",
        "📌 第4步: 其他服务",
        "   - ElastiCache: cache.m6g.large × 3",
        "   - DMS: dms.t3.large × 1",
        "   - S3: Standard 500GB",
        "   - CloudWatch & X-Ray",
        "   - Secrets Manager & KMS",
        "",
        "📌 预计总成本: ${:,.2f}/月".format(total_monthly),
        "",
        "📢 提示: 所有配置完成后, 点击'保存'即可生成分享链接"
    ]
    
    for instruction in instructions:
        print(instruction)
    
    # 生成配置文件供导入
    config_file = f"aws_calculator_config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    with open(config_file, "w") as f:
        json.dump(import_config, f, indent=2, ensure_ascii=False)
    
    print(f"\n📁 配置已保存到: {config_file}")
    print("📋 您可以将此配置手动应用到Pricing Calculator")
    
    print("\n" + "=" * 80)
    print("📧 如果需要生成直接的API链接，需要AWS账户授权")
    print("=" * 80)

if __name__ == "__main__":
    main()