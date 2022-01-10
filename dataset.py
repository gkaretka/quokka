import pandas as pd
from io import BytesIO, StringIO
import boto3
import time
class InputCSVDataset:

    def __init__(self, bucket, key, names, id) -> None:

        self.s3 = boto3.client('s3') # needs boto3 client
        self.bucket = bucket
        self.key = key
        self.num_mappers = None
        self.names = names
        self.id = id
    
    def set_num_mappers(self, num_mappers):
        self.num_mappers = num_mappers
        self.length, self.adjusted_splits = self.get_csv_attributes()

    def get_csv_attributes(self, window = 1024):

        if self.num_mappers is None:
            raise Exception
        splits = 16

        response = self.s3.head_object(
            Bucket=self.bucket,
            Key=self.key
        )
        length = response['ContentLength']
        assert length // splits > window * 2
        potential_splits = [length//splits * i for i in range(splits)]
        #adjust the splits now
        adjusted_splits = []

        # the first value is going to be the start of the second row 
        # -- we assume there's a header and skip it!

        resp = self.s3.get_object(Bucket=self.bucket,Key=self.key, Range='bytes={}-{}'.format(0, window))['Body'].read()

        first_newline = resp.find(bytes('\n','utf-8'))
        if first_newline == -1:
            raise Exception
        else:
            adjusted_splits.append(first_newline)

        for i in range(1, len(potential_splits)):
            potential_split = potential_splits[i]
            start = max(0,potential_split - window)
            end = min(potential_split + window, length)
            
            resp = self.s3.get_object(Bucket=self.bucket,Key=self.key, Range='bytes={}-{}'.format(start, end))['Body'].read()
            last_newline = resp.rfind(bytes('\n','utf-8'))
            if last_newline == -1:
                raise Exception
            else:
                adjusted_splits.append(start + last_newline)
        
        adjusted_splits[-1] = length 
        
        print(length, adjusted_splits)
        return length, adjusted_splits

    def get_next_batch(self, mapper_id, stride=1024 * 1024 * 64): #default is to get 16 KB batches at a time. 
        
        if self.num_mappers is None:
            raise Exception("I need to know the total number of mappers you are planning on using.")

        splits = len(self.adjusted_splits)
        assert self.num_mappers < splits + 1
        assert mapper_id < self.num_mappers + 1
        chunks = splits // self.num_mappers
        start = self.adjusted_splits[chunks * mapper_id]
        
        if mapper_id == self.num_mappers - 1:
            end = self.adjusted_splits[splits - 1]
        else:
            end = self.adjusted_splits[chunks * mapper_id + chunks] 

        pos = start
        while pos < end-1:

            resp = self.s3.get_object(Bucket=self.bucket,Key=self.key, Range='bytes={}-{}'.format(pos,min(pos+stride,end)))['Body'].read()

            last_newline = resp.rfind(bytes('\n','utf-8'))

            #import pdb;pdb.set_trace()
            
            if last_newline == -1:
                raise Exception
            else:
                resp = resp[:last_newline]
                pos += last_newline
                print("start convert,",time.time())
                bump = pd.read_csv(BytesIO(resp), names =self.names )
                print("done convert,",time.time())
                yield bump


class OutputCSVDataset:

    def __init__(self, bucket, key, id) -> None:

        self.s3 = boto3.client('s3') # needs boto3 client
        self.bucket = bucket
        self.key = key
        self.id = id
        self.num_reducers = None

        self.multipart_upload = self.s3.create_multipart_upload(
            Bucket=bucket,
            Key=key,
        )

        self.parts = []

    def set_num_reducer(self, num_reducer):
        self.num_reducers = num_reducer
        chunks = 10000 // num_reducer
        self.reducer_current_part = [(i * chunks + 1) for i in range(num_reducer)]
        self.reducer_partno_limit = self.reducer_current_part[1:] + [10000]

    def upload_chunk(self, df, reducer_id):
        if self.num_reducers is None:
            raise Exception("I need to know the number of reducers")

        current_part = self.reducer_current_part[reducer_id]
        if current_part == self.reducer_partno_limit[reducer_id] - 1:
            raise Exception("ran out of part numbers")
        
        csv_buffer = StringIO()
        df.to_csv(csv_buffer, header = (True if current_part == 1 else False))
        
        uploadPart = self.s3.upload_part(
            Bucket = self.bucket, 
            Key = self.key, 
            UploadId = self.multipart_upload['UploadId'],
            PartNumber = current_part,
            Body = csv_buffer.getvalue()
        )

        self.reducer_current_part[reducer_id] += 1
        
        self.parts.append({
            'PartNumber': current_part,
            'ETag': uploadPart['ETag']
        })
        
