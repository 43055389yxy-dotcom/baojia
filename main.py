#!/usr/bin/env python3
"""
AWS云服务报价自动化系统
根据用户需求自动验证AWS配置并生成官方报价
"""

import os
import sys
import json
import boto3
import pandas as pd
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# AWS 配置。凭证由环境变量、EC2 Instance Role 或本机 AWS 配置自动提供。
AWS_CONFIG = {
    'region_name': os.getenv('AWS_DEFAULT_REGION', 'us-east-1')
}

class AWSQuoter:
    """AWS报价引擎"""
    
    def __init__(self):
        """初始化AWS客户端"""
        self.session = boto3.Session(
            region_name=AWS_CONFIG['region_name']
        )
        
        # 初始化AWS客户端
        self.ec2_client = self.session.client('ec2')
        self.pricing_client = self.session.client('pricing', region_name='us-east-1')
        self.s3_client = self.session.client('s3')
        self.rds_client = self.session.client('rds')
        
        # 缓存数据
        self.instance_types_cache = None
        self.region_cache = None
        self.service_codes_cache = None
        
        logger.info("AWS Quoter初始化完成")
    
    def validate_credentials(self) -> bool:
        """验证AWS凭证有效性"""
        try:
            sts = self.session.client('sts')
            identity = sts.get_caller_identity()
            logger.info(f"AWS账号验证成功 - Account: {identity['Account']}, User: {identity['UserId']}")
            return True
        except Exception as e:
            logger.error(f"AWS凭证验证失败: {e}")
            return False
    
    def get_regions(self) -> List[Dict]:
        """获取可用区域列表"""
        if self.region_cache:
            return self.region_cache
            
        try:
            response = self.ec2_client.describe_regions()
            regions = response['Regions']
            self.region_cache = regions
            logger.info(f"获取到{len(regions)}个AWS区域")
            return regions
        except Exception as e:
            logger.error(f"获取区域列表失败: {e}")
            return []
    
    def get_instance_types(self, region: str = 'us-east-1') -> List[str]:
        """获取指定区域的实例类型"""
        try:
            # 临时切换到指定区域
            ec2_regional = self.session.client('ec2', region_name=region)
            response = ec2_regional.describe_instance_types()
            instance_types = [inst['InstanceType'] for inst in response['InstanceTypes']]
            logger.info(f"在{region}区域获取到{len(instance_types)}个实例类型")
            return instance_types
            
        except Exception as e:
            logger.error(f"获取实例类型失败: {e}")
            # 返回常用实例类型作为备选
            return [
                't3.micro', 't3.small', 't3.medium', 't3.large', 't3.xlarge',
                'm5.large', 'm5.xlarge', 'm5.2xlarge', 'm5.4xlarge',
                'c5.large', 'c5.xlarge', 'c5.2xlarge', 'c5.4xlarge',
                'r5.large', 'r5.xlarge', 'r5.2xlarge', 'r5.4xlarge'
            ]
    
    def get_price_for_instance(self, instance_type: str, region: str = 'us-east-1', 
                              os_type: str = 'Linux') -> Optional[Dict]:
        """获取实例类型的价格"""
        try:
            # 使用Price List API查询价格
            filters = [
                {
                    'Type': 'TERM_MATCH',
                    'Field': 'instanceType',
                    'Value': instance_type
                },
                {
                    'Type': 'TERM_MATCH',
                    'Field': 'operatingSystem',
                    'Value': os_type
                },
                {
                    'Type': 'TERM_MATCH',
                    'Field': 'regionCode',
                    'Value': region
                },
                {
                    'Type': 'TERM_MATCH',
                    'Field': 'tenancy',
                    'Value': 'Shared'
                },
                {
                    'Type': 'TERM_MATCH',
                    'Field': 'preInstalledSw',
                    'Value': 'NA'
                },
                {
                    'Type': 'TERM_MATCH',
                    'Field': 'capacitystatus',
                    'Value': 'Used'
                }
            ]
            
            # 查询按需实例价格
            response = self.pricing_client.get_products(
                ServiceCode='AmazonEC2',
                Filters=filters,
                FormatVersion='aws_v1',
                MaxResults=100
            )
            
            if response['PriceList']:
                price_item = json.loads(response['PriceList'][0])
                
                # 解析价格信息
                price_dimensions = price_item.get('terms', {}).get('OnDemand', {})
                for sku, term_details in price_dimensions.items():
                    price_dimension = list(term_details['priceDimensions'].values())[0]
                    
                    price_info = {
                        'instance_type': instance_type,
                        'region': region,
                        'operating_system': os_type,
                        'price_per_hour': float(price_dimension['pricePerUnit']['USD']),
                        'unit': price_dimension['unit'],
                        'description': price_dimension.get('description', ''),
                        'sku': sku
                    }
                    
                    return price_info
            
            return None
            
        except Exception as e:
            logger.error(f"查询实例{instance_type}价格失败: {e}")
            return None
    
    def analyze_customer_requirements(self, requirements_text: str) -> Dict:
        """分析客户需求文本"""
        logger.info(f"开始分析客户需求: {requirements_text[:100]}...")
        
        requirements = {
            'services': [],
            'regions': [],
            'instance_types': [],
            'quantities': {},
            'storage_requirements': {},
            'network_requirements': {},
            'errors': [],
            'warnings': []
        }
        
        # 服务关键词匹配
        service_keywords = {
            'ec2': ['EC2', '实例', '虚拟机', '服务器', '计算', 'vm'],
            's3': ['S3', '存储', '对象存储', 'bucket'],
            'rds': ['RDS', '数据库', 'mysql', 'postgresql', 'sql'],
            'elasticache': ['ElastiCache', 'redis', 'memcached', '缓存'],
            'cloudfront': ['CloudFront', 'cdn', '内容分发'],
            'lambda': ['Lambda', '函数', '无服务器'],
            'ebs': ['EBS', '块存储', '磁盘'],
            'elb': ['ELB', '负载均衡', '负载均衡器']
        }
        
        # 区域关键词匹配
        region_patterns = {
            'us-east-1': ['us-east-1', 'us east', 'n. virginia', 'virginia', '弗吉尼亚'],
            'us-west-2': ['us-west-2', 'us west', 'oregon', '俄勒冈'],
            'eu-west-1': ['eu-west-1', 'eu west', 'ireland', '爱尔兰'],
            'ap-southeast-1': ['ap-southeast-1', 'singapore', '新加坡'],
            'ap-northeast-1': ['ap-northeast-1', 'tokyo', '东京']
        }
        
        # 实例类型匹配
        text_lower = requirements_text.lower()
        
        # 识别服务
        for service, keywords in service_keywords.items():
            for keyword in keywords:
                if keyword.lower() in text_lower:
                    if service not in requirements['services']:
                        requirements['services'].append(service)
                    break
        
        # 识别区域
        for region_code, patterns in region_patterns.items():
            for pattern in patterns:
                if pattern.lower() in text_lower:
                    if region_code not in requirements['regions']:
                        requirements['regions'].append(region_code)
                    break
        
        # 识别实例类型 (简化的正则匹配)
        import re
        instance_matches = re.findall(r'\b([ctmrgp]\d+\.[a-z]+\d*)\b', text_lower, re.IGNORECASE)
        requirements['instance_types'] = [match.upper() for match in instance_matches]
        
        # 识别数量
        quantity_matches = re.findall(r'(\d+)\s*(台|个|套|instance|台服务器|个实例)', text_lower)
        if quantity_matches:
            for qty, unit in quantity_matches:
                requirements['quantities']['servers'] = int(qty)
                break
        
        # 识别存储需求
        storage_matches = re.findall(r'(\d+)\s*(GB|gb|G|g|TB|tb|T|t)\s*(存储|硬盘|disk|storage)', text_lower)
        if storage_matches:
            for size, unit, _ in storage_matches:
                size_val = int(size)
                if unit.upper() in ['TB', 'T']:
                    size_val *= 1000
                requirements['storage_requirements']['size_gb'] = size_val
                break
        
        logger.info(f"需求分析结果: {requirements}")
        return requirements
    
    def validate_configuration(self, requirements: Dict) -> Dict:
        """验证配置的有效性"""
        validation_result = {
            'valid': True,
            'issues': [],
            'suggestions': [],
            'validated_instances': []
        }
        
        # 验证区域
        available_regions = self.get_regions()
        region_names = [r['RegionName'] for r in available_regions]
        
        for req_region in requirements.get('regions', []):
            if req_region not in region_names:
                validation_result['valid'] = False
                validation_result['issues'].append(f"区域 '{req_region}' 不可用")
                # 建议替代区域
                if 'us-east-1' in region_names:
                    validation_result['suggestions'].append(f"建议使用 us-east-1 替代 {req_region}")
        
        # 验证实例类型
        for region in requirements.get('regions', ['us-east-1']):
            available_instances = self.get_instance_types(region)
            
            for instance_type in requirements.get('instance_types', []):
                if instance_type not in available_instances:
                    validation_result['valid'] = False
                    validation_result['issues'].append(f"实例类型 '{instance_type}' 在区域 '{region}' 不可用")
                    
                    # 寻找最接近的实例类型
                    suggestions = self.find_closest_instances(instance_type, available_instances)
                    validation_result['suggestions'].append(f"实例 '{instance_type}' 不可用，建议: {suggestions}")
                else:
                    validation_result['validated_instances'].append({
                        'instance_type': instance_type,
                        'region': region,
                        'valid': True
                    })
        
        return validation_result
    
    def find_closest_instances(self, target_instance: str, available_instances: List[str]) -> List[str]:
        """寻找最接近的实例类型"""
        suggestions = []
        
        # 解析实例类型家族
        family_match = re.match(r'([a-z]+)\d+', target_instance.lower())
        if family_match:
            family = family_match.group(1)
            
            # 寻找同家族的实例
            same_family = [inst for inst in available_instances if inst.startswith(family.upper())]
            
            if same_family:
                # 按大小排序
                same_family.sort(key=lambda x: self.get_instance_size_value(x))
                target_size = self.get_instance_size_value(target_instance)
                
                # 寻找低一档的
                lower_tier = [inst for inst in same_family if self.get_instance_size_value(inst) < target_size]
                if lower_tier:
                    suggestions.append(f"低一档: {lower_tier[-1]}")
                
                # 寻找高一档的
                higher_tier = [inst for inst in same_family if self.get_instance_size_value(inst) > target_size]
                if higher_tier:
                    suggestions.append(f"高一档: {higher_tier[0]}")
        
        return suggestions
    
    def get_instance_size_value(self, instance_type: str) -> int:
        """获取实例大小数值用于排序"""
        size_map = {
            'nano': 1,
            'micro': 2,
            'small': 3,
            'medium': 4,
            'large': 5,
            'xlarge': 6,
            '2xlarge': 7,
            '4xlarge': 8,
            '8xlarge': 9,
            '12xlarge': 10,
            '16xlarge': 11,
            '24xlarge': 12,
            '32xlarge': 13,
            '48xlarge': 14,
            '64xlarge': 15
        }
        
        # 提取大小部分
        for size_name, value in size_map.items():
            if size_name in instance_type.lower():
                return value
        
        return 5  # 默认值
    
    def generate_quotation(self, requirements: Dict, validated_config: Dict) -> pd.DataFrame:
        """生成报价表"""
        quotation_items = []
        
        # 为每个验证通过的实例生成报价
        for instance_info in validated_config.get('validated_instances', []):
            instance_type = instance_info['instance_type']
            region = instance_info['region']
            
            # 获取价格
            price_info = self.get_price_for_instance(instance_type, region)
            
            if price_info:
                # 计算月费 (按730小时/月)
                monthly_cost = price_info['price_per_hour'] * 730
                
                item = {
                    'AWS服务': 'Amazon EC2',
                    '区域': region,
                    '型号': instance_type,
                    '数量': requirements.get('quantities', {}).get('servers', 1),
                    '配置': f"{instance_type} - Linux/Unix",
                    '计价单位': price_info['unit'],
                    '官方参考单价': f"${price_info['price_per_hour']:.4f}/小时",
                    '月费': f"${monthly_cost:.2f}",
                    '备注': '按需实例'
                }
                
                quotation_items.append(item)
        
        # 转换为DataFrame
        if quotation_items:
            df = pd.DataFrame(quotation_items)
            
            # 计算合计
            total = sum(float(item['月费'].replace('$', '')) for item in quotation_items)
            
            # 添加合计行
            total_row = {
                'AWS服务': '合计',
                '区域': '',
                '型号': '',
                '数量': '',
                '配置': '',
                '计价单位': '',
                '官方参考单价': '',
                '月费': f"${total:.2f}",
                '备注': ''
            }
            
            return df
            
        return pd.DataFrame()
    
    def save_to_excel(self, dataframe: pd.DataFrame, filename: str = None) -> str:
        """保存报价到Excel文件"""
        if filename is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"exports/aws_quotation_{timestamp}.xlsx"
        
        # 确保exports目录存在
        os.makedirs('exports', exist_ok=True)
        
        # 保存到Excel
        excel_writer = pd.ExcelWriter(filename, engine='openpyxl')
        dataframe.to_excel(excel_writer, index=False, sheet_name='AWS报价')
        
        # 调整列宽
        worksheet = excel_writer.sheets['AWS报价']
        for column_cells in worksheet.columns:
            length = max(len(str(cell.value)) for cell in column_cells)
            worksheet.column_dimensions[column_cells[0].column_letter].width = length + 2
        
        excel_writer.close()
        
        logger.info(f"报价已保存到: {filename}")
        return filename


def main():
    """主函数"""
    print("=" * 60)
    print("AWS云服务报价自动化系统")
    print("=" * 60)
    
    # 初始化报价引擎
    quoter = AWSQuoter()
    
    # 验证凭证
    if not quoter.validate_credentials():
        print("错误: AWS凭证验证失败，请检查凭证是否正确")
        return
    
    print("✅ AWS凭证验证成功")
    
    # 示例客户需求
    sample_requirements = """
    我们需要在东京区域部署3台EC2实例：
    - 2台 c5.xlarge 实例用于应用服务器
    - 1台 r5.xlarge 实例用于数据库
    - 每台实例需要500GB SSD存储
    - 操作系统使用Linux
    - 需要美国东部作为备份区域
    """
    
    print("\n📋 示例客户需求:")
    print(sample_requirements)
    
    # 分析需求
    requirements = quoter.analyze_customer_requirements(sample_requirements)
    
    print("\n🔍 需求分析结果:")
    print(f"服务: {requirements['services']}")
    print(f"区域: {requirements['regions']}")
    print(f"实例类型: {requirements['instance_types']}")
    print(f"数量: {requirements['quantities']}")
    print(f"存储需求: {requirements['storage_requirements']}")
    
    # 验证配置
    print("\n🔧 配置验证...")
    validation = quoter.validate_configuration(requirements)
    
    if not validation['valid']:
        print("\n⚠️ 配置验证发现问题:")
        for issue in validation['issues']:
            print(f"  - {issue}")
        
        print("\n💡 解决方案建议:")
        for suggestion in validation['suggestions']:
            print(f"  - {suggestion}")
    else:
        print("✅ 配置验证通过")
    
    # 生成报价
    if validation['validated_instances']:
        print("\n💰 生成报价...")
        quotation_df = quoter.generate_quotation(requirements, validation)
        
        if not quotation_df.empty:
            print("\n📊 报价明细:")
            print(quotation_df.to_string(index=False))
            
            # 保存到Excel
            excel_file = quoter.save_to_excel(quotation_df)
            print(f"\n💾 报价已保存到: {excel_file}")
        else:
            print("⚠️ 无法生成报价，价格信息获取失败")
    else:
        print("\n⚠️ 没有有效的实例配置可用于报价")
    
    print("\n" + "=" * 60)
    print("报价流程完成")
    print("=" * 60)


if __name__ == "__main__":
    # 导入re模块
    import re
    
    try:
        main()
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"\n发生错误: {e}")
        logger.exception("程序运行异常")
