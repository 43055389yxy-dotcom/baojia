"""Customer-facing copy only; never a catalog, product resolver or price rule."""

from app.domain.models import ConfirmationItem

# Labels explain already-offered official resource types, without inventing
# availability or adding choices. Source: AWS Backup supported resources:
# https://docs.aws.amazon.com/aws-backup/latest/devguide/whatisbackup.html#supported-resources
BACKUP_RESOURCE_COPY = {
    "amazonEfsBackup": ("共享文件系统（EFS）", "备份 Amazon EFS 中共享的文件和目录。"),
    "vMwareBackup": ("虚拟机（VMware）", "备份 VMware 虚拟机；不是选择普通 EC2 云服务器。"),
    "timestreamBackup": ("时序数据库（Timestream）", "备份 Timestream for LiveAnalytics 数据表，不适用于 InfluxDB。"),
    "storageGatewayBackup": ("存储网关卷（Storage Gateway）", "备份 AWS Storage Gateway 卷网关中的存储卷。"),
    "fsxBackup": ("托管文件系统（FSx）", "备份 Amazon FSx 文件系统中的数据。"),
    "s3Backup": ("对象存储（S3）", "备份 S3 存储桶中的文件和对象；不是指把其他资源的备份存到 S3。"),
    "redshiftBackup": ("数据仓库（Redshift）", "备份 Amazon Redshift 数据仓库集群。"),
    "rdsBackup": ("关系型数据库（RDS）", "备份 Amazon RDS 数据库；Aurora 请选单独的 Aurora 选项。"),
    "ebsBackup": ("云硬盘（EBS）", "备份 EBS 存储卷，例如 EC2 云服务器使用的云硬盘。"),
    "backupDynamoDb": ("键值数据库（DynamoDB）", "备份 Amazon DynamoDB 数据表。"),
    "auroraBackup": ("云数据库（Aurora）", "备份 Amazon Aurora 数据库集群。"),
    "neptuneBackup": ("图数据库（Neptune）", "备份 Amazon Neptune 数据库集群。"),
    "docDbBackup": ("文档数据库（DocumentDB）", "备份 Amazon DocumentDB 基于实例的数据库集群。"),
    "sapHanaBackup": ("企业数据库（SAP HANA）", "备份运行在 Amazon EC2 上的 SAP HANA 数据库。"),
    "auroraDsqlBackup": ("分布式数据库（Aurora DSQL）", "备份 Amazon Aurora DSQL 集群，与普通 Aurora 区分。"),
}


def customer_confirmation_item(item: ConfirmationItem) -> ConfirmationItem:
    """Decorate both new and saved questions without changing answer identity."""
    if not item.options or not all(o.value.startswith("official_template:") for o in item.options):
        return item
    is_backup = item.service == "backup"
    question = (
        "这项 AWS Backup 要备份哪种资源？请选择被备份的对象，不是备份存放的位置。"
        if is_backup else "这项需求实际使用哪种资源类型？请选择与你的业务对应的一项。"
    )
    options = []
    for option in item.options:
        code = option.value.removeprefix("official_template:")
        label, description = BACKUP_RESOURCE_COPY.get(code, (
            option.label, "请选择实际使用的资源；不确定时，请向维护该系统的同事确认。",
        )) if is_backup else (option.label, "请选择实际使用的资源类型。")
        options.append(option.model_copy(update={"label": label, "description": description}))
    return item.model_copy(update={"question": question, "options": options,
                                   "answer_key": item.answer_key or item.question})
